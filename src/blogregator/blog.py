import json
import os
import time
import sys

from typing import Annotated
from urllib.parse import urlparse

import requests
import typer

from bs4 import BeautifulSoup
from litellm import completion

from blogregator.database import get_connection
from blogregator.prompts import GENERATE_JSON_PROMPT
from blogregator.parser import parse_with_config

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

def fetch_html_body(url, retries=3, sleep=1):
    """Fetch the HTML <body> content from a URL."""
    attempts = 0
    while attempts < retries:
        try:
            response = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            return str(soup.body)

        except requests.RequestException as e:
            print(f"Error fetching the URL: {e}")
            attempts += 1
            time.sleep(sleep)
    raise requests.RequestException(f'Unable to retrieve content from page: {url}')

def generate_schema(html_content, url):
    """Use Gemini to generate a parser function for the blog."""
    formatted_prompt = GENERATE_JSON_PROMPT.format(html_content=html_content, blog_url=url)
    
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY environment variable is not set")
            sys.exit(1)
            
        response = completion(
            model="gemini/gemini-2.0-flash",
            messages=[{"role": "user", "content": formatted_prompt}],
            api_key=api_key
        )
        
        # Extract the generated json from the response
        result: str = response.choices[0].message.content # type: ignore
        
        # Clean up the response - remove markdown code blocks if present
        if "```json" in result:
            result = result.split("```json")[1]
            if "```" in result:
                result = result.split("```")[0]
                
        # Parse to JSON
        schema = json.loads(result)
        return schema
    
    except Exception as e:
        print(f"Error generating parser function: {e}")
        sys.exit(1)

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
    cursor.execute("SELECT COUNT(*) FROM blogs WHERE url = ?", (url,))
    count = cursor.fetchone()[0]
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
    body = fetch_html_body(url)

    typer.echo('Generating parser function...')

    schema = generate_schema(body, url)
    typer.echo('JSON Schema -----')
    typer.echo(schema)
    typer.echo('-----------------')
    
    _ = parse_with_config(url, schema)

    typer.echo('Saving parser function to file...')

    typer.echo('Adding blog to database...')
    cursor.execute(
        """INSERT INTO blogs (name, url, scraping_schema, status)
        VALUES (?, ?, ?, ?)""",
        (name, url, json.dumps(schema), 'Inactive')
            )
    conn.commit()
    conn.close()