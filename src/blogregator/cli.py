import json

from sqlite3 import Cursor
from typing import Annotated

import typer

from blogregator.blog import blog_cli
from blogregator.database import init_db, get_connection
from blogregator.parser import parse_with_config
from blogregator.utils import utcnow

app = typer.Typer()
app.add_typer(blog_cli, name='blog', help="Commands for managing individual blogs.")

@app.command()
def init():
    """Initialize SQLite DB and create tables."""
    init_db()
    typer.echo("Database initialized.")


def fetch_blogs(cursor: Cursor, blog_id: int | None):
    """Retrieve active blogs or a specific blog by ID."""
    if blog_id is not None:
        cursor.execute("SELECT * FROM blogs WHERE id = ?", (blog_id,))
    else:
        cursor.execute("SELECT * FROM blogs WHERE status = 'Active'")
    return cursor.fetchall()

def add_post(cursor: Cursor, blog_id: int, post_info: dict[str, str]):
    """Add a post to the database if it isn't already registered."""
    values = (
        blog_id, post_info['title'], post_info['post_url'], post_info.get('content'),
        post_info.get('html'), post_info['date'], utcnow().isoformat()
        )
    cursor.execute(
        "INSERT OR IGNORE INTO posts (blog_id, title, url, content, html_content, publication_date, discovered_date)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        values
    )

def log_error(cursor: Cursor, blog_id: int, error_type: str, message: str):
    """Insert an error log entry."""
    cursor.execute(
        "INSERT INTO error_log (blog_id, timestamp, error_type, message) VALUES (?, ?, ?, ?)",
        (blog_id, utcnow().isoformat(), error_type, message)
    )

def process_blog(cursor, blog):
    """Run scraper for a single blog and handle results."""
    typer.echo(f"Checking blog '{blog['name']}' (ID {blog['id']})...")
    try:
        posts = parse_with_config(blog['url'], json.loads(blog['scraping_schema']))
    except Exception as e:
        log_error(cursor, blog['id'], 'network', str(e))
        return {'success': 0, 'network': 1, 'parsing': 0}

    metrics = {'success': 0, 'network': 0, 'parsing': 0}
    for p in posts:
        try:
            add_post(cursor, blog['id'], p)
            metrics['success'] += 1
        except Exception as e:
            log_error(cursor, blog['id'], 'parsing', str(e))
            cursor.execute(
                "UPDATE blogs SET status = 'Error' WHERE id = ?", (blog['id'],)
            )
            typer.echo(typer.style(
                f"Disabled blog {blog['id']} due to parsing error: {e}",
                fg=typer.colors.RED
            ))
            metrics['parsing'] += 1
            break
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
        cursor.execute("SELECT COUNT(*) FROM blogs WHERE status = 'Active'")
        total = cursor.fetchone()[0]
        if not yes and not typer.confirm(f"You're about to check {total} blogs. Continue?"):
            typer.echo("Aborted.")
            conn.close()
            return

    blogs = fetch_blogs(cursor, blog_id)
    totals = {'success': 0, 'network': 0, 'parsing': 0}

    for blog in blogs:
        metrics = process_blog(cursor, blog)
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
        "SELECT id, title, publication_date, url FROM posts WHERE blog_id = ? "
        "ORDER BY publication_date DESC LIMIT ?", (blog_id, limit)
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
        "FROM posts p LEFT JOIN metadata m ON p.id = m.post_id WHERE p.id = ?", (post_id,)
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
