import json
import multiprocessing as mp
import os

from functools import partial

from typing import Annotated, Any

import psycopg2
import psycopg2.extras
import typer

from blogregator.blog import blog_cli
from blogregator.database import get_connection
from blogregator.parser import parse_post_list
from blogregator.post import extract_post_metadata
from blogregator.utils import utcnow

app = typer.Typer()
app.add_typer(blog_cli, name='blog', help="Commands for managing individual blogs.")

def fetch_blogs(cursor, blog_id: int | None):
    """Retrieve active blogs or a specific blog by ID."""
    if blog_id is not None:
        cursor.execute("SELECT * FROM blogs WHERE id = %s", (blog_id,))
    else:
        cursor.execute("SELECT * FROM blogs WHERE status = %s", ('Active',))
    return cursor.fetchall()


def add_post(cursor, blog_id: int, post_info: dict[str, str], metadata: dict[str, Any]):
    """Add a post to the database if it isn't already registered."""
    print(metadata)
    cursor.execute(
        """INSERT INTO posts (blog_id, title, url, publication_date, reading_time, summary)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            blog_id, post_info['title'], post_info['post_url'], post_info['date'],
            metadata['reading_time'], metadata['summary']
        )
    )
    post_id = cursor.fetchone()['id']
    topics = metadata.get('matched_topics', []) + metadata.get('new_topic_suggestions', [])
    print(topics)

    # get the IDs of each topic
    cursor.execute("SELECT id FROM topics WHERE name = ANY(%s)", (topics,))
    topic_ids = [row['id'] for row in cursor.fetchall()]
    print(topic_ids)

    psycopg2.extras.execute_values(
        cursor,
        """INSERT INTO post_topics (post_id, topic_id) VALUES %s ON CONFLICT DO NOTHING""",
        [(post_id, topic_id) for topic_id in topic_ids]
    )


def log_error(cursor, blog_id: int, error_type: str, message: str):
    """Insert an error log entry."""
    cursor.execute(
        "INSERT INTO error_log (blog_id, timestamp, error_type, message) VALUES (%s, %s, %s, %s)",
        (blog_id, utcnow().isoformat(), error_type, message)
    )

def process_single_post(post, blog_id):
    local_conn = get_connection()  # Each process needs its own connection
    local_cursor = local_conn.cursor()
    local_metrics = {'success': 0, 'network': 0, 'parsing': 0}
    
    try:
        typer.echo(f"Processing post: {post['post_url']}")
        metadata = extract_post_metadata(post['post_url'])
        
        # Add new topics to database if any
        if metadata.get('new_topic_suggestions'):
            psycopg2.extras.execute_values(
                local_cursor,
                "INSERT INTO topics (name) VALUES %s ON CONFLICT DO NOTHING",
                [(t,) for t in metadata['new_topic_suggestions']]
            )
        
        # Add the post
        add_post(local_cursor, blog_id, post, metadata)
        local_metrics['success'] += 1
        local_conn.commit()
    except Exception as e:
        # Log the error but don't disable the blog immediately
        # We'll decide whether to disable after all posts are processed
        log_error(local_cursor, blog_id, 'parsing', str(e))
        local_metrics['parsing'] += 1
        # No raise here - we want to keep processing other posts
    finally:
        local_conn.close()
        
    return local_metrics

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


@app.command(name="view-posts")
def view_posts(
    blog_id: int = typer.Argument(..., help="ID of the blog"),
    limit: int = typer.Option(10, "-n", help="Max number of posts to display")
):
    """View recent posts for a specific blog."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, publication_date, url FROM posts WHERE blog_id = %s "
        "ORDER BY publication_date DESC LIMIT %s", (blog_id, limit)
    )
    posts = cursor.fetchall()
    conn.close()

    if not posts:
        typer.echo("No posts found for this blog.")
        return

    typer.echo(f"{'Post ID':<8} {'Published':<20} {'Title'}")
    for p in posts:
        pub = p['publication_date']
        typer.echo(f"{p['id']:<8} {pub:<20} {p['title']}")

@app.command(name="view-post")
def view_post(
    post_id: int = typer.Argument(..., help="ID of the post to view")
):
    """View detailed information for a single post."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT p.title, p.url, p.content, p.html_content, p.publication_date, m.topics, m.reading_time, m.summary "
        "FROM posts p LEFT JOIN metadata m ON p.id = m.post_id WHERE p.id = %s", (post_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        typer.echo("Post not found.")
        return

    typer.echo(typer.style(row['title'], bold=True))
    typer.echo(f"URL: {row['url']}")
    typer.echo(f"Published: {row['publication_date']}")
    typer.echo("\nContent:\n" + (row['content'] or "[No content]"))
    if row['topics']:
        typer.echo(f"\nTopics: {row['topics']}")
    if row['reading_time']:
        typer.echo(f"Reading time: {row['reading_time']} min")
    if row['summary']:
        typer.echo("\nSummary:\n" + row['summary'])

if __name__ == "__main__":
    app()
