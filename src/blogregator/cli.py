import json
import multiprocessing as mp
import os

from functools import partial

from typing import Annotated

import typer

from blogregator.blog import blog_cli
from blogregator.database import get_connection, init_database, log_error
from blogregator.emails import notify
from blogregator.parser import parse_post_list
from blogregator.post import post_cli, process_single_post

app = typer.Typer()
app.add_typer(blog_cli, name='blog', help="Commands for managing blogs.")
app.add_typer(post_cli, name='post', help="Commands for managing posts.")

@app.command(name='send-newsletter')
def send_newsletter(hour_window: Annotated[int, typer.Option(help="Number of hours to look back for new posts")] = 8):
    """Send an email with new posts discovered in the last hour_window hours."""
    typer.echo(f"Looking for posts from the past {hour_window} hours...")
    try:
        n_posts = notify(hour_window)
        if n_posts == 0:
            typer.echo("No new posts found.")
            return
        else:
            typer.echo(typer.style(f"Newsletter with {n_posts} posts sent successfully", fg=typer.colors.GREEN))
    except Exception as e:
        typer.echo(typer.style(f"Failed to send newsletter: {e}", fg=typer.colors.RED))
        raise e
        
@app.command(name="init-db")
def init_db():
    """Initialize the database by creating tables from a schema file."""
    try:
        init_database()
        typer.echo(typer.style("Database initialized successfully!", fg=typer.colors.GREEN))
    except Exception as e:
        typer.echo(typer.style(f"Failed to initialize database: {e}", fg=typer.colors.RED))

def fetch_blogs(cursor, blog_id: int | None):
    """Retrieve active blogs or a specific blog by ID."""
    if blog_id is not None:
        cursor.execute("SELECT * FROM blogs WHERE id = %s", (blog_id,))
    else:
        cursor.execute("SELECT * FROM blogs WHERE status = %s", ('Active',))
    return cursor.fetchall()


def process_blog(conn, blog):
    """Run scraper for a single blog and handle results."""
    cursor = conn.cursor()
    typer.echo(f"Checking blog '{blog['name']}' (ID {blog['id']})...")
    try:
        posts = parse_post_list(blog['url'], json.loads(blog['scraping_schema']))
    except Exception as e:
        log_error(cursor, blog['id'], 'network', str(e))
        return {'success': 0, 'network': 1, 'parsing': 0}

    metrics = {'success': 0, 'network': 0, 'parsing': 0}
    
    post_urls = [p['post_url'] for p in posts]
    cursor.execute("SELECT url FROM posts WHERE url = ANY(%s)", (post_urls,))
    existing = {row['url'] for row in cursor.fetchall()}
    
    new_posts = [p for p in posts if p['post_url'] not in existing]
    
    typer.echo(f"Found {len(new_posts)} new posts.")


    # Determine optimal number of workers based on CPU count and post count
    max_workers = min(os.cpu_count() or 4, len(new_posts))
    # Ensure we don't create too many processes for just a few posts
    if max_workers > 8:
        max_workers = 8
    
    combined_metrics = {'success': 0, 'network': 0, 'parsing': 0}
    error_encountered = False
    
    # Use ProcessPoolExecutor for parallel processing
    if new_posts:
        with mp.Pool(processes=max_workers) as pool:
            # Map the function to process posts in parallel
            process_func = partial(process_single_post, blog_id=blog['id'])
            results = list(pool.map(process_func, new_posts))
            
            # Combine metrics from all workers
            for result in results:
                for key in combined_metrics:
                    combined_metrics[key] += result[key]
                if result['parsing'] > 0:
                    error_encountered = True
    
    # Update metrics with combined results
    metrics.update(combined_metrics)
    
    # If any parsing errors occurred, disable the blog
    if error_encountered:
        cursor.execute(
            "UPDATE blogs SET status = %s WHERE id = %s", ('Error', blog['id'])
        )
        typer.echo(typer.style(
            f"Disabled blog {blog['id']} due to parsing errors",
            fg=typer.colors.RED
        ))
    
    return metrics


@app.command(name="run-check")
def run_check(
        blog_id: Annotated[int | None, typer.Option(help="ID of a specific blog to check")] = None,
        yes: Annotated[bool, typer.Option("-y", help="Skip confirmation for checking all blogs")] = False
      ):
    """Run one-off check for new posts."""
    conn = get_connection()
    cursor = conn.cursor()

    # Confirmation if all blogs are asked to be checked
    if blog_id is None:
        cursor.execute("SELECT COUNT(*) FROM blogs WHERE status = %s", ('Active',))
        total = cursor.fetchone()['count']
        if not yes and not typer.confirm(f"You're about to check {total} blogs. Continue?"):
            typer.echo("Aborted.")
            conn.close()
            return

    blogs = fetch_blogs(cursor, blog_id)
    totals = {'success': 0, 'network': 0, 'parsing': 0}

    for blog in blogs:
        metrics = process_blog(conn, blog)
        conn.commit()
        for key, value in metrics.items():
            totals[key] += value

    typer.echo(typer.style(
        (f"Done. Posts added: {totals['success']}, "
        f"network errors: {totals['network']}, "
        f"parsing errors: {totals['parsing']}"),
        fg=typer.colors.BLUE
    ))

    conn.close()




if __name__ == "__main__":
    app()
