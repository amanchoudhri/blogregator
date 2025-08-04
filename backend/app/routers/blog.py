import json
import datetime

from typing import Annotated, List, Dict, Any, Optional
from urllib.parse import urlparse

from psycopg import Connection
from psycopg.rows import class_row

from fastapi import APIRouter, Body, HTTPException, status

from pydantic import HttpUrl
import typer

from bs4 import BeautifulSoup

from app.dependencies import CurrentUser
from app.rate_limit import email_rate_limit

from ..database import get_connection
from ..llm import generate_json_from_llm
from ..prompts import GENERATE_SCHEMA, REFINE_SCHEMA
from ..parser import parse_post_list
from ..utils import fetch_with_retries, utcnow
from ..models import Blog

blog_cli = typer.Typer(
    name="blog",
    help="Manage and interact with blogs in the registry."
)

router = APIRouter(prefix="/blogs")

MAX_REFINEMENT_ATTEMPTS = 3


@router.get("/")
def fetch_blogs():
    """Return all indexed blogs."""
    with get_connection() as conn:
        cur = conn.execute("""
            SELECT id, url, scraping_schema, scraping_successful, ticket_open FROM blogs"""
        )
        blogs = cur.fetchall()

    return {"blogs": blogs}

@router.post("/new")
def add_new_blog(user: CurrentUser, blog_link: Annotated[HttpUrl, Body()]):
    with get_connection() as db_conn:
        with db_conn.cursor(row_factory=class_row(Blog)) as cursor:
            search_match = f"%{blog_link.host}%"
            cursor.execute(
                """SELECT * FROM blogs WHERE url LIKE %s""",
                (search_match,)
                )
            matches = cursor.fetchall()

            if matches is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='Blog already added.'
                    )

            content = fetch_with_retries(blog_link).content
            body = str(BeautifulSoup(content, 'html.parser').body)

            print(f'Generating parser function for blog {blog_link}...')

            schema = generate_schema(body, blog_link)

            cursor.execute(
                """INSERT INTO blogs (url, scraping_schema, last_modified_by)
                VALUES (%s, %s, %s)""",
                (str(blog_link), json.dumps(schema), user.id)
            )

            posts = parse_post_list(str(blog_link), schema)

        return {"schema": schema, "posts": posts}

@router.post("/{blog_id}/refine")
def refine_blog_schema(user: CurrentUser, blog_id: int, feedback: Annotated[str, Body()]):
    email_rate_limit("5/5minutes; 10/hour", "/blog/refine", user.email)

    with get_connection() as db_conn:
        blog = check_blog_exists(db_conn, blog_id)

        if blog.scraping_successful:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="Blog has a working scraping schema, refining not permitted"
                )
        if has_open_ticket(db_conn, blog.id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "A support ticket is open for this blog, refining not permitted."
                )

        if blog.refinement_attempts >= MAX_REFINEMENT_ATTEMPTS:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Max refinement attempts reached on this blog. Please submit a support ticket."
                )

        content = fetch_with_retries(blog.url).content
        body = str(BeautifulSoup(content, 'html.parser').body)

        existing_schema: dict = json.loads(blog.scraping_schema)
        existing_results = parse_post_list(str(blog.url), existing_schema)

        print(f'Refining parser function for blog {blog.url}...')

        new_schema = refine_schema(
                existing_schema,
                existing_results,
                feedback,
                body,
                str(blog.url)
        )

        reparsed_posts = parse_post_list(str(blog.url), new_schema)

        db_conn.execute("""
            UPDATE blogs SET
                refinement_attempts = refinement_attempts + 1,
                proposed_schema = %s,
                last_modified_by = %s,
                last_modified_at = NOW()
            WHERE id = %s""",
            (new_schema, user.id, blog.id)
        )

        return {"schema": new_schema, "posts": reparsed_posts}

@router.post("/{blog_id}/apply-refinement")
def apply_refinement(user: CurrentUser, blog_id: int):
    """
    Set the proposed schema for a blog to the main schema,
    if one exists.
    """
    with get_connection() as db_conn:
        blog = check_blog_exists(db_conn, blog_id)

        if not blog.proposed_schema:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="No refined schema for this blog exists to apply."
                )

        if blog.last_modified_by != user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="User does not have permission to apply refinement."
                )

        try:
            db_conn.execute("""
                UPDATE blogs SET
                   scraping_schema = proposed_schema,
                   proposed_schema = '',
                   last_modified_at = NOW()
                WHERE id = %s""",
                (blog.id,)
            )
            return {"detail": "Refinement successfully applied."}
        except:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Blog scraping status was not able to be updated. Please try again."
                )

@router.post("/{blog_id}/confirm")
def confirm_scraping_schema(user: CurrentUser, blog_id: int):
    """
    Confirm that a generated schema for a blog is successful.

    Available only for 24 hours to the user who last modified a blog entry
    (either `new` or `refine`), and only if there is no admin ticket open.
    """
    with get_connection() as db_conn:
        blog = check_blog_exists(db_conn, blog_id)

        is_right_user = blog.last_modified_by = user.id
        is_within_window = blog.last_modified_at > (utcnow() + datetime.timedelta(hours=24))
        if not (is_right_user and is_within_window):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                'User does not have permission to confirm blog scraping schema.'
                )

        if has_open_ticket(db_conn, blog.id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                ("A support ticket is open for this blog's scraping schema, "
                 "so it cannot be marked as successful.")
                )

        try:
            db_conn.execute(
                "UPDATE blogs SET scraping_successful = TRUE WHERE id = %s",
                (user.id,)
            )
            return {"detail": "Blog updated successfully."}
        except:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Blog scraping status was not able to be updated. Please try again."
                )

@router.post("/{blog_id}/ticket")
def open_ticket(user: CurrentUser, blog_id: int, message: Annotated[str, Body()]):
    with get_connection() as db_conn:
        check_blog_exists(db_conn, blog_id)
        # open the ticket
        db_conn.execute(
            "INSERT INTO tickets (blog_id, opened_by, message) VALUES (%s, %s, %s)",
            (blog_id, user.id, message)
            )
        # set the scraping schema as unsuccessful on the blog
        db_conn.execute("""
            UPDATE blogs SET
                scraping_successful = FALSE,
                last_modified_by = %s,
                last_modified_at = NOW()
            WHERE id = %s
            """,
            (user.id, blog_id)
            )

def check_blog_exists(db_conn: Connection[dict], blog_id: int) -> Blog:
    """
    Check if a blog exists; if not, throws a 404 error.
    """
    with db_conn.cursor(row_factory=class_row(Blog)) as cur:
        cur.execute("SELECT * FROM blogs WHERE id = %s", (blog_id,))
        blog = cur.fetchone()

    if blog is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Blog not found.')

    return blog

def has_open_ticket(db_conn: Connection[dict], blog_id: int) -> bool:
    blog = check_blog_exists(db_conn, blog_id)

    with db_conn.cursor() as cur:
        n_tickets = cur.execute(
            "SELECT COUNT(*) FROM tickets WHERE blog_id = %s AND resolved = FALSE",
            (blog.id,)
            ).fetchone()

    return n_tickets != 0
    

def generate_schema(html_content, url):
    """Use Gemini to generate a parser function for the blog."""
    formatted_prompt = GENERATE_SCHEMA.format(html_content=html_content, blog_url=url)
    return generate_json_from_llm(formatted_prompt)

def refine_schema(
    previous_schema: dict,
    previous_results: list[dict],
    user_feedback: str,
    html_content: str,
    url: str
    ):
    formatted_previous_results = "\n\n".join(
        format_post_for_display(post, i) for i, post in enumerate(previous_results, 1)
        )
    formatted_prompt = REFINE_SCHEMA.format(
        previous_schema=json.dumps(previous_schema, indent=2),
        previous_results=formatted_previous_results,
        blog_url=url,
        html_content=html_content,
        user_feedback=user_feedback
    )
    return generate_json_from_llm(formatted_prompt)

def get_domain_name(url: str) -> str:
    """
    Return the main domain name of a URL (without 'www.' or any subdomains/TLDs).
    """
    # Parse out the network location part
    netloc = urlparse(url).netloc
    
    # Remove port if present (e.g. 'example.com:8080')
    hostname = netloc.split(':')[0]
    
    # Strip leading 'www.' if it’s there
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    
    # Take the first segment before any remaining dots
    main_domain = hostname.split('.')[0]
    
    return main_domain

def format_post_date(post: Dict[str, Any]) -> Optional[str]:
    """Format the post date consistently."""
    pub_date = post.get('date')
    if not pub_date:
        return None
    
    try:
        return datetime.datetime.strptime(pub_date, '%Y-%m-%d').strftime('%Y-%m-%d')
    except:
        return pub_date

def format_post_for_display(post: Dict[str, Any], index: int) -> str:
    """Format a single post for display in the console."""
    result = f"Post {index}:\n"
    result += f"Title: {post.get('title', 'No title found')}\n"
    result += f"URL: {post.get('post_url', 'No URL found')}\n"
    
    pub_date = format_post_date(post)
    if pub_date:
        result += f"Date: {pub_date}\n"
    
    return result

def display_posts(posts: List[Dict[str, Any]], message: str = "Found posts:") -> None:
    """Display posts in a consistent format."""
    typer.echo(f"\n{message}")
    for i, post in enumerate(posts, 1):
        typer.echo(f"\nPost {i}:")
        typer.echo(f"Title: {post.get('title', 'No title found')}")
        typer.echo(f"URL: {post.get('post_url', 'No URL found')}")
        
        pub_date = format_post_date(post)
        if pub_date:
            typer.echo(f"Date: {pub_date}")

def save_blog_to_database(conn, name, url, schema, status='Active', update_existing=False):
    """Save the blog information to the database.
    
    Args:
        conn: Database connection
        name: Blog name
        url: Blog URL
        schema: Scraping schema (JSON)
        status: Blog status ('Active', 'Error', etc.)
        update_existing: If True, update existing blog instead of inserting new one
    """
    typer.echo(f'Saving blog to database with status: {status}...')
    
    cursor = conn.cursor()
    
    if update_existing:
        cursor.execute(
            """UPDATE blogs 
            SET name = %s, scraping_schema = %s, status = %s
            WHERE url = %s""",
            (name, json.dumps(schema), status, url)
        )
        typer.echo(f"Successfully updated blog: {name} ({url})")
    else:
        cursor.execute(
            """INSERT INTO blogs (name, url, scraping_schema, status)
            VALUES (%s, %s, %s, %s)""",
            (name, url, json.dumps(schema), status)
        )
        typer.echo(f"Successfully added blog: {name} ({url})")
    
    conn.commit()

@blog_cli.command("add")
def add_blog(
        url: Annotated[str, typer.Argument(help="The URL of the blog to add.")],
        name: Annotated[str | None, typer.Option(
            help="A string name describing the blog. Will be generated from URL if not provided."
            )] = None
        ):
    
    # check if blog already in database
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM blogs WHERE url = %s", (url,))
    count = cursor.fetchone()['count'] # type: ignore
    if count > 0:
        if not typer.confirm(f"Blog with URL {url} already exists. Overwrite?"):
            conn.close()
            return
        typer.echo(f"Updating existing blog: {url}")
        update_existing = True
    else:
        update_existing = False
    
    typer.echo(f'Adding blog: {url}')

    if name is None:
        name = get_domain_name(url)

    # TODO: error handling
    typer.echo('Fetching HTML content...')
    content = fetch_with_retries(url).content
    body = str(BeautifulSoup(content, 'html.parser').body)

    typer.echo('Generating parser function...')

    schema = generate_schema(body, url)
    typer.echo('JSON Schema -----')
    typer.echo(schema)
    typer.echo('-----------------')
    
    # First attempt: Parse posts with the initial schema
    first_attempt_success = False
    error = ""
    posts = []
    
    try:
        posts = parse_post_list(url, schema)
        if posts:
            first_attempt_success = True
            display_posts(posts, "Found posts using the generated schema:")
        else:
            typer.echo("No posts were found using the generated schema.")
    except Exception as e:
        typer.echo(f"Failed to parse posts: {e}")
        error = str(e)
    
    # If first attempt was successful, ask for confirmation
    if first_attempt_success:
        if typer.confirm("\nDoes this look correct?"):
            save_blog_to_database(conn, name, url, schema, update_existing=update_existing)
            return
        
    user_feedback = typer.prompt("Please provide feedback on what went wrong")
    
    # If we get here, we need to try to improve the schema
    typer.echo("Attempting to generate an improved schema...")
    
    # Format the previous results for display (if any)
    previous_results = []
    if posts:
        previous_results = [format_post_for_display(post, i) for i, post in enumerate(posts, 1)]
    
    # Generate a new schema using the correction prompt
    formatted_prompt = REFINE_SCHEMA.format(
        previous_schema=json.dumps(schema, indent=2),
        previous_results="\n\n".join(previous_results),
        blog_url=url,
        html_content=body,
        error=error,
        user_feedback=user_feedback
    )
    
    # Second attempt: Try with an improved schema
    improved_schema = None
    improved_posts = []
    
    try:
        improved_schema = generate_json_from_llm(formatted_prompt)

        typer.echo('Improved JSON Schema -----')
        typer.echo(improved_schema)
        typer.echo('-----------------')

        typer.echo("\nTrying the improved schema...")
        
        improved_posts = parse_post_list(url, improved_schema)
        if improved_posts:
            display_posts(improved_posts, "Found posts using the improved schema:")
        else:
            typer.echo("Still no posts found with the improved schema.")
    except Exception as e:
        typer.echo(f"Error with improved schema: {e}")
    
    # If we got an improved schema (even if it had errors), ask if user wants to save it
    if improved_schema:
        status = 'Active' if improved_posts else "Error"
        save_blog_to_database(conn, name, url, improved_schema, status=status, update_existing=update_existing)
    else:
        # No improved schema was generated
        typer.echo("Failed to generate an improved schema.")
        if typer.confirm("\nSave the original schema with an error status?"):
            save_blog_to_database(conn, name, url, schema, status="Error", update_existing=update_existing)
        else:
            typer.echo("Aborting blog addition. No schema saved.")
            conn.close()
    conn.close()
