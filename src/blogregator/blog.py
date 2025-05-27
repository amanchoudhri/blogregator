import json
import datetime

from typing import Annotated, List, Dict, Any, Optional
from urllib.parse import urlparse

import typer

from bs4 import BeautifulSoup

from blogregator.database import get_connection
from blogregator.llm import generate_json_from_llm
from blogregator.prompts import GENERATE_SCHEMA, CORRECT_SCHEMA
from blogregator.parser import parse_post_list
from blogregator.utils import fetch_with_retries

blog_cli = typer.Typer(
    name="blog",
    help="Manage and interact with blogs in the registry."
)

@blog_cli.command(name="list")
def list_blogs():
    """List all monitored blogs with status and last checked date."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, url, status, last_checked FROM blogs ORDER BY id")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        typer.echo("No blogs found.")
        return

    typer.echo(f"{'ID':<4} {'Name':<20} {'Status':<10} {'Last Checked'}")
    for r in rows:
        last = r['last_checked'] or 'Never'
        typer.echo(f"{r['id']:<4} {r['name']:<20} {r['status']:<10} {last}")

def generate_schema(html_content, url):
    """Use Gemini to generate a parser function for the blog."""
    formatted_prompt = GENERATE_SCHEMA.format(html_content=html_content, blog_url=url)
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

def save_blog_to_database(conn, name, url, schema, status='Active'):
    """Save the blog information to the database.
    
    Args:
        conn: Database connection
        name: Blog name
        url: Blog URL
        schema: Scraping schema (JSON)
        status: Blog status ('Active', 'Error', etc.)
    """
    typer.echo(f'Saving blog to database with status: {status}...')
    
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO blogs (name, url, scraping_schema, status)
        VALUES (%s, %s, %s, %s)""",
        (name, url, json.dumps(schema), status)
    )
    conn.commit()
    conn.close()
    
    typer.echo(f"Successfully added blog: {name} ({url})")

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
    count = cursor.fetchone()['count']
    if count > 0:
        if not typer.confirm(f"Blog with URL {url} already exists. Overwrite?"):
            conn.close()
            return
        cursor.execute("DELETE FROM blogs WHERE url = %s", (url,))
        conn.commit()
    
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
            save_blog_to_database(conn, name, url, schema)
            return
        
    user_feedback = typer.prompt("Please provide feedback on what went wrong")
    
    # If we get here, we need to try to improve the schema
    typer.echo("Attempting to generate an improved schema...")
    
    # Format the previous results for display (if any)
    previous_results = []
    if posts:
        previous_results = [format_post_for_display(post, i) for i, post in enumerate(posts, 1)]
    
    # Generate a new schema using the correction prompt
    formatted_prompt = CORRECT_SCHEMA.format(
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
        save_blog_to_database(conn, name, url, improved_schema, status=status)
    else:
        # No improved schema was generated
        typer.echo("Failed to generate an improved schema.")
        if typer.confirm("\nSave the original schema with an error status?"):
            save_blog_to_database(conn, name, url, schema, status="Error")
        else:
            typer.echo("Aborting blog addition. No schema saved.")
            conn.close()
    conn.close()
