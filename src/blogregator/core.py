"""Core business logic for blog checking and newsletter sending."""

import json
import logging
import multiprocessing as mp
import os
from dataclasses import dataclass

import psycopg2.extras

from blogregator.database import get_connection, log_error
from blogregator.emails import notify
from blogregator.parser import parse_post_list
from blogregator.post import add_post_to_db, process_single_post

logger = logging.getLogger(__name__)


@dataclass
class CheckMetrics:
    """Metrics from a blog check operation."""

    new_posts_found: int = 0
    full_success: int = 0
    partial_success: int = 0
    network_errors: int = 0
    llm_missing_summary: int = 0
    llm_missing_reading_time: int = 0
    llm_missing_topics: int = 0


@dataclass
class CheckResult:
    """Result of checking blogs for new posts."""

    success: bool
    blogs_checked: int
    total_metrics: CheckMetrics
    blog_parse_errors: int
    error_message: str | None = None


def fetch_blogs(cursor, blog_id: int | None):
    """Retrieve active blogs or a specific blog by ID."""
    if blog_id is not None:
        cursor.execute("SELECT * FROM blogs WHERE id = %s", (blog_id,))
    else:
        # Use scraping_successful to determine active blogs
        cursor.execute("SELECT * FROM blogs WHERE scraping_successful = true", ())
    return cursor.fetchall()


def process_blog(conn, blog, max_workers: int = 8) -> CheckMetrics:
    """
    Run scraper for a single blog and handle results.

    Args:
        conn: Database connection
        blog: Blog record from database
        max_workers: Maximum number of parallel workers for processing posts

    Returns:
        CheckMetrics with statistics about the operation
    """
    cursor = conn.cursor()
    # Extract blog name from URL for logging
    blog_name = blog["url"].split("//")[-1].split("/")[0] if blog.get("url") else "Unknown"
    logger.info(
        f"Checking blog: {blog_name} (ID: {blog['id']})",
        extra={
            "blog_id": blog["id"],
            "blog_name": blog_name,
            "blog_url": blog["url"],
        },
    )

    # Parse post list - blog parse errors disable the entire blog
    try:
        posts = parse_post_list(blog["url"], json.loads(blog["scraping_schema"]))
    except Exception as e:
        logger.error(
            "Blog parsing failed",
            extra={
                "blog_id": blog["id"],
                "blog_name": blog_name,
                "error": str(e),
            },
            exc_info=True,
        )
        log_error(cursor, blog["id"], "blog_parse", str(e))
        # Mark blog as unsuccessful
        cursor.execute(
            "UPDATE blogs SET scraping_successful = false, last_checked = NOW() WHERE id = %s",
            (blog["id"],),
        )
        return CheckMetrics()

    # Find new posts
    post_urls = [p["post_url"] for p in posts]
    cursor.execute("SELECT url FROM posts WHERE url = ANY(%s)", (post_urls,))
    existing = {row["url"] for row in cursor.fetchall()}

    new_posts = [p for p in posts if p["post_url"] not in existing]

    logger.info(
        f"Found {len(new_posts)} new post(s) for {blog_name} (total on page: {len(posts)})",
        extra={
            "blog_id": blog["id"],
            "blog_name": blog_name,
            "new_posts_count": len(new_posts),
            "total_posts_on_page": len(posts),
        },
    )

    # Process posts in parallel with timeout protection
    results = []
    if new_posts:
        max_workers_actual = min(os.cpu_count() or 4, len(new_posts), max_workers)
        logger.info(
            f"Processing {len(new_posts)} new post(s) for {blog_name} with {max_workers_actual} worker(s)",
            extra={
                "blog_id": blog["id"],
                "blog_name": blog_name,
                "new_posts_count": len(new_posts),
                "workers": max_workers_actual,
            },
        )

        # Use imap_unordered with timeout to prevent hanging on stuck posts
        # Each post gets up to 2 minutes (Playwright timeout + buffer)
        per_post_timeout = 120
        with mp.Pool(processes=max_workers_actual) as pool:
            async_results = pool.imap_unordered(process_single_post, new_posts)
            for _ in new_posts:
                try:
                    result = async_results.next(timeout=per_post_timeout)
                    results.append(result)
                except mp.TimeoutError:
                    logger.warning(
                        f"Post processing timed out after {per_post_timeout}s",
                        extra={"blog_id": blog["id"], "blog_name": blog_name},
                    )
                except StopIteration:
                    break

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
            logger.warning(
                "LLM extraction failed for post",
                extra={
                    "blog_id": blog["id"],
                    "post_url": result.original_post.get("post_url"),
                    "error": result.error_message,
                },
            )
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

    # Update last_checked timestamp and mark as successful
    cursor.execute(
        "UPDATE blogs SET scraping_successful = true, last_checked = NOW() WHERE id = %s",
        (blog["id"],),
    )

    logger.info(
        f"Completed processing blog {blog_name}: saved {len(posts_to_save)} post(s)",
        extra={
            "blog_id": blog["id"],
            "blog_name": blog_name,
            "posts_saved": len(posts_to_save),
        },
    )

    # Calculate detailed metrics
    metrics = CheckMetrics(
        new_posts_found=len(new_posts),
        full_success=sum(1 for r in results if r.success),
        partial_success=sum(
            1 for r in results if not r.success and any([r.summary, r.reading_time, r.topics])
        ),
        network_errors=sum(1 for r in results if r.error_type == "network"),
        llm_missing_summary=sum(1 for r in results if not r.summary and r.error_type != "network"),
        llm_missing_reading_time=sum(
            1 for r in results if not r.reading_time and r.error_type != "network"
        ),
        llm_missing_topics=sum(1 for r in results if not r.topics and r.error_type != "network"),
    )

    return metrics


def run_blog_check(blog_id: int | None = None, max_workers: int = 8) -> CheckResult:
    """
    Run one-off check for new posts.

    Args:
        blog_id: Optional specific blog ID to check. If None, checks all active blogs.
        max_workers: Maximum number of parallel workers for processing posts

    Returns:
        CheckResult with summary of the operation
    """
    mode = "single blog" if blog_id else "all blogs"
    logger.info(
        f"Starting blog check ({mode})",
        extra={"blog_id": blog_id, "mode": "single" if blog_id else "all"},
    )

    try:
        conn = get_connection()
        cursor = conn.cursor()

        blogs = fetch_blogs(cursor, blog_id)
        if not blogs:
            logger.warning("No blogs found to check", extra={"blog_id": blog_id})
            conn.close()
            return CheckResult(
                success=True,
                blogs_checked=0,
                total_metrics=CheckMetrics(),
                blog_parse_errors=0,
            )

        logger.info(f"Found {len(blogs)} blog(s) to check", extra={"count": len(blogs)})

        totals = CheckMetrics()
        blog_parse_errors = 0

        for blog in blogs:
            try:
                metrics = process_blog(conn, blog, max_workers=max_workers)
                conn.commit()

                # Aggregate metrics
                totals.new_posts_found += metrics.new_posts_found
                totals.full_success += metrics.full_success
                totals.partial_success += metrics.partial_success
                totals.network_errors += metrics.network_errors
                totals.llm_missing_summary += metrics.llm_missing_summary
                totals.llm_missing_reading_time += metrics.llm_missing_reading_time
                totals.llm_missing_topics += metrics.llm_missing_topics

                # Check if blog was disabled due to parse errors
                if (
                    metrics.new_posts_found == 0
                    and metrics.full_success == 0
                    and metrics.partial_success == 0
                ):
                    cursor.execute(
                        "SELECT scraping_successful FROM blogs WHERE id = %s", (blog["id"],)
                    )  # type: ignore
                    status_row = cursor.fetchone()
                    if status_row and not status_row["scraping_successful"]:  # type: ignore
                        blog_parse_errors += 1

            except Exception as e:
                blog_name_err = (
                    blog["url"].split("//")[-1].split("/")[0] if blog.get("url") else "Unknown"
                )
                logger.error(
                    "Error processing blog",
                    extra={"blog_id": blog["id"], "blog_name": blog_name_err, "error": str(e)},  # type: ignore
                    exc_info=True,
                )
                conn.rollback()
                # Continue processing other blogs

        conn.close()

        posts_added = totals.full_success + totals.partial_success
        logger.info(
            f"Blog check completed: {len(blogs)} blog(s) checked, {totals.new_posts_found} new post(s) found, {posts_added} post(s) added",
            extra={
                "blogs_checked": len(blogs),
                "new_posts_found": totals.new_posts_found,
                "posts_added": posts_added,
                "blog_parse_errors": blog_parse_errors,
            },
        )

        return CheckResult(
            success=True,
            blogs_checked=len(blogs),
            total_metrics=totals,
            blog_parse_errors=blog_parse_errors,
        )

    except Exception as e:
        logger.error("Blog check failed", extra={"error": str(e)}, exc_info=True)
        return CheckResult(
            success=False,
            blogs_checked=0,
            total_metrics=CheckMetrics(),
            blog_parse_errors=0,
            error_message=str(e),
        )


def send_newsletter_if_needed(hour_window: int = 6) -> tuple[bool, int, str | None]:
    """
    Send newsletter if there are new posts in the specified time window.

    Args:
        hour_window: Number of hours to look back for new posts

    Returns:
        Tuple of (success, number_of_posts_sent, error_message)
    """
    logger.info("Checking for new posts to send in newsletter", extra={"hour_window": hour_window})

    try:
        n_posts = notify(hour_window)

        if n_posts == 0:
            logger.info("No new posts found for newsletter")
            return (True, 0, None)
        else:
            logger.info("Newsletter sent successfully", extra={"posts_count": n_posts})
            return (True, n_posts, None)

    except Exception as e:
        logger.error("Failed to send newsletter", extra={"error": str(e)}, exc_info=True)
        return (False, 0, str(e))
