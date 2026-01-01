import json
import multiprocessing as mp
import os
from typing import Annotated

import psycopg2.extras
import typer

from blogregator.blog import blog_cli
from blogregator.database import get_connection, init_database, log_error
from blogregator.emails import notify
from blogregator.parser import parse_post_list
from blogregator.post import add_post_to_db, post_cli, process_single_post

app = typer.Typer()
app.add_typer(blog_cli, name="blog", help="Commands for managing blogs.")
app.add_typer(post_cli, name="post", help="Commands for managing posts.")


@app.command(name="send-newsletter")
def send_newsletter(
    hour_window: Annotated[
        int, typer.Option(help="Number of hours to look back for new posts")
    ] = 8,
):
    """Send an email with new posts discovered in the last hour_window hours."""
    typer.echo(f"Looking for posts from the past {hour_window} hours...")
    try:
        n_posts = notify(hour_window)
        if n_posts == 0:
            typer.echo("No new posts found.")
            return
        else:
            typer.echo(
                typer.style(
                    f"Newsletter with {n_posts} posts sent successfully", fg=typer.colors.GREEN
                )
            )
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
        cursor.execute("SELECT * FROM blogs WHERE status = %s", ("Active",))
    return cursor.fetchall()


def process_blog(conn, blog):
    """Run scraper for a single blog and handle results."""
    cursor = conn.cursor()
    typer.echo(f"Checking blog '{blog['name']}' (ID {blog['id']})...")

    # Parse post list - blog parse errors disable the entire blog
    try:
        posts = parse_post_list(blog["url"], json.loads(blog["scraping_schema"]))
    except Exception as e:
        log_error(cursor, blog["id"], "blog_parse", str(e))
        cursor.execute("UPDATE blogs SET status = %s WHERE id = %s", ("Error", blog["id"]))
        return {
            "new_posts_found": 0,
            "full_success": 0,
            "partial_success": 0,
            "network_errors": 0,
            "llm_breakdown": {"missing_summary": 0, "missing_reading_time": 0, "missing_topics": 0},
        }

    # Find new posts
    post_urls = [p["post_url"] for p in posts]
    cursor.execute("SELECT url FROM posts WHERE url = ANY(%s)", (post_urls,))
    existing = {row["url"] for row in cursor.fetchall()}

    new_posts = [p for p in posts if p["post_url"] not in existing]

    typer.echo(f"Found {len(new_posts)} new posts.")

    # Process posts in parallel
    results = []
    if new_posts:
        max_workers = min(os.cpu_count() or 4, len(new_posts), 8)

        with mp.Pool(processes=max_workers) as pool:
            results = list(pool.map(process_single_post, new_posts))

    # Batch database operations
    posts_to_save = []
    all_topics = set()

    for result in results:
        # Only save posts with at least one LLM field populated
        if any([result.summary, result.reading_time, result.topics]):
            posts_to_save.append(result)
            if result.topics:
                all_topics.update(result.topics)

        # Log LLM errors for debugging
        if result.error_type == "llm":
            log_error(cursor, blog["id"], "llm", result.error_message or "LLM extraction failed")

    # Add new topics first
    if all_topics:
        psycopg2.extras.execute_values(
            cursor,
            "INSERT INTO topics (name) VALUES %s ON CONFLICT DO NOTHING",
            [(topic,) for topic in all_topics],
        )

    # Add posts to database
    for result in posts_to_save:
        metadata = {
            "summary": result.summary,
            "reading_time": result.reading_time,
            "matched_topics": result.topics or [],
            "new_topic_suggestions": [],
        }
        add_post_to_db(
            cursor,
            blog["id"],
            result.original_post,
            metadata,
            full_text=result.extracted_text,
        )

    # Calculate detailed metrics
    metrics = {
        "new_posts_found": len(new_posts),
        "full_success": sum(1 for r in results if r.success),
        "partial_success": sum(
            1 for r in results if not r.success and any([r.summary, r.reading_time, r.topics])
        ),
        "network_errors": sum(1 for r in results if r.error_type == "network"),
        "llm_breakdown": {
            "missing_summary": sum(
                1 for r in results if not r.summary and r.error_type != "network"
            ),
            "missing_reading_time": sum(
                1 for r in results if not r.reading_time and r.error_type != "network"
            ),
            "missing_topics": sum(1 for r in results if not r.topics and r.error_type != "network"),
        },
    }

    return metrics


@app.command(name="run-check")
def run_check(
    blog_id: Annotated[int | None, typer.Option(help="ID of a specific blog to check")] = None,
    yes: Annotated[
        bool, typer.Option("-y", help="Skip confirmation for checking all blogs")
    ] = False,
):
    """Run one-off check for new posts."""
    conn = get_connection()
    cursor = conn.cursor()

    # Confirmation if all blogs are asked to be checked
    if blog_id is None:
        cursor.execute("SELECT COUNT(*) FROM blogs WHERE status = %s", ("Active",))
        total = cursor.fetchone()["count"]
        if not yes and not typer.confirm(f"You're about to check {total} blogs. Continue?"):
            typer.echo("Aborted.")
            conn.close()
            return

    blogs = fetch_blogs(cursor, blog_id)
    totals = {
        "new_posts_found": 0,
        "full_success": 0,
        "partial_success": 0,
        "network_errors": 0,
        "llm_breakdown": {"missing_summary": 0, "missing_reading_time": 0, "missing_topics": 0},
    }
    blog_parse_errors = 0

    for blog in blogs:
        metrics = process_blog(conn, blog)
        conn.commit()

        # Aggregate metrics
        for key in ["new_posts_found", "full_success", "partial_success", "network_errors"]:
            totals[key] += metrics[key]

        for key in totals["llm_breakdown"]:
            totals["llm_breakdown"][key] += metrics["llm_breakdown"][key]

        # Check if blog was disabled due to parse errors
        if (
            metrics["new_posts_found"] == 0
            and metrics["full_success"] == 0
            and metrics["partial_success"] == 0
        ):
            cursor.execute("SELECT status FROM blogs WHERE id = %s", (blog["id"],))
            if cursor.fetchone()["status"] == "Error":
                blog_parse_errors += 1

    # Generate detailed output
    posts_added = totals["full_success"] + totals["partial_success"]
    complete_count = totals["full_success"]
    partial_count = totals["partial_success"]

    status_parts = []
    if posts_added > 0:
        if complete_count > 0 and partial_count > 0:
            status_parts.append(
                f"added: {posts_added} ({complete_count} complete, {partial_count} partial)"
            )
        elif complete_count > 0:
            status_parts.append(f"added: {posts_added} (all complete)")
        else:
            status_parts.append(f"added: {posts_added} (all partial)")

    if totals["network_errors"] > 0:
        status_parts.append(f"Network errors: {totals['network_errors']}")

    # LLM failure details
    llm_failures = []
    for field, count in totals["llm_breakdown"].items():
        if count > 0:
            field_name = field.replace("missing_", "")
            llm_failures.append(f"{count} missing {field_name}")

    if llm_failures:
        status_parts.append(f"LLM failures: {', '.join(llm_failures)}")

    if blog_parse_errors > 0:
        status_parts.append(
            f"Blog parse errors: {blog_parse_errors} blog{'s' if blog_parse_errors > 1 else ''} disabled"
        )

    summary = f"Done. Posts found: {totals['new_posts_found']}"
    if status_parts:
        summary += ", " + ", ".join(status_parts)
    else:
        summary += ", no changes needed"

    typer.echo(typer.style(summary, fg=typer.colors.BLUE))

    conn.close()


if __name__ == "__main__":
    app()
