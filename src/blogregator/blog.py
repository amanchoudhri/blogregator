import json
import datetime

from typing import Annotated
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
        cursor.execute("DELETE FROM blogs WHERE url = ?", (url,))
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
    
    # Get and display parsed posts
    posts = parse_post_list(url, schema)
    
    if not posts:
        typer.echo("No posts were found using the generated schema.")
        conn.close()
        return
    
    typer.echo("\nFound posts using the generated schema:")
    for i, post in enumerate(posts, 1):
        typer.echo(f"\nPost {i}:")
        typer.echo(f"Title: {post.get('title', 'No title found')}")
        typer.echo(f"URL: {post.get('post_url', 'No URL found')}")
        pub_date = post.get('date')
        if pub_date:
            try:
                pub_date = datetime.datetime.strptime(pub_date, '%Y-%m-%d').strftime('%Y-%m-%d')
            except:
                pass
            typer.echo(f"Date: {pub_date}")
        
    if not typer.confirm("\nDoes this look correct? (If not, the schema will need to be adjusted)"):
        typer.echo("Previous schema didn't work well. Let's try generating a better one...")
        
        # Format the previous results for display
        previous_results = []
        for i, post in enumerate(posts, 1):
            result = f"Post {i}:\n"
            result += f"Title: {post.get('title', 'No title found')}\n"
            result += f"URL: {post.get('post_url', 'No URL found')}\n"
            pub_date = post.get('date')
            if pub_date:
                try:
                    pub_date = datetime.datetime.strptime(pub_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                except:
                    pass
                result += f"Date: {pub_date}\n"
            previous_results.append(result)
        
        # Generate a new schema using the correction prompt
        formatted_prompt = CORRECT_SCHEMA.format(
            previous_schema=json.dumps(schema, indent=2),
            previous_results="\n\n".join(previous_results),
            blog_url=url,
            html_content=body
        )
        
        try:
            schema = _get_json_from_llm(formatted_prompt)
            
            typer.echo("\nTrying the improved schema...")
            posts = parse_post_list(url, schema)
            
            if not posts:
                typer.echo("Still no posts found. Please manually adjust the schema and try again.")
                conn.close()
                return
            
            typer.echo("\nFound posts using the improved schema:")
            for i, post in enumerate(posts, 1):
                typer.echo(f"\nPost {i}:")
                typer.echo(f"Title: {post.get('title', 'No title found')}")
                typer.echo(f"URL: {post.get('post_url', 'No URL found')}")
                pub_date = post.get('date')
                if pub_date:
                    try:
                        pub_date = datetime.datetime.strptime(pub_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                    except:
                        pass
                    typer.echo(f"Date: {pub_date}")
            
            if not typer.confirm("\nDoes this look better? If not, you'll need to manually adjust the schema."):
                typer.echo("Aborting blog addition. Please refine the schema and try again.")
                conn.close()
                return
            
        except Exception as e:
            print(f"Error generating improved schema: {e}")
            typer.echo("Failed to generate an improved schema. Please manually adjust the schema and try again.")
            conn.close()
            return
    
    typer.echo('Saving parser function to file...')

    typer.echo('Adding blog to database...')
    cursor.execute(
        """INSERT INTO blogs (name, url, scraping_schema, status)
        VALUES (%s, %s, %s, %s)""",
        (name, url, json.dumps(schema), 'Inactive')
            )
    conn.commit()
    conn.close()
