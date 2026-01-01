#!/usr/bin/env python3
"""
Backfill Script: Re-extract summaries and topics for posts with missing metadata.

This script was created on 2025-12-30 to fix posts that were added when the Gemini
model was misconfigured. The issue was:

PROBLEM:
- Posts discovered between ~18:05-18:10 on 2025-12-30 had missing summaries and topics
- The LLM extraction was using "gemini-2.5-flash-preview-05-20" which was deprecated
- All LLM calls failed with 404 errors
- Posts were still saved because reading_time extraction succeeded (word count based)
- Result: 12 posts with reading_time but NULL summary and empty topics

ROOT CAUSE:
- Model "gemini-2.5-flash-preview-05-20" no longer exists in Gemini API
- Error: "models/gemini-2.5-flash-preview-05-20 is not found for API version v1beta"

FIX APPLIED:
- Updated model to "gemini-3-flash-preview" in:
  - src/blogregator/llm.py (line 17)
  - src/blogregator/post.py (lines 300, 384, 394)
- Fixed .env file quotes for SMTP_PORT and DATABASE_URL
- Rebuilt and restarted Docker container

This script re-extracts metadata for affected posts using the working model.

Usage:
    # Backfill posts from last 3 hours
    python examples/backfill_posts.py --hours 3

    # Backfill specific post by ID
    python examples/backfill_posts.py --post-id 499

    # Dry run (show what would be updated)
    python examples/backfill_posts.py --hours 3 --dry-run
"""

import argparse
import multiprocessing as mp
import os
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from blogregator.database import get_connection
from blogregator.post import extract_post_text, extract_summary, extract_topics
from blogregator.utils import fetch_with_retries

# Default number of parallel workers
DEFAULT_WORKERS = 8


def get_posts_needing_backfill(
    cursor,
    hours: int | None = None,
    post_id: int | None = None,
    full_text_only: bool = False,
):
    """
    Find posts that need metadata backfill.

    Args:
        cursor: Database cursor
        hours: Only look at posts discovered in last N hours
        post_id: Specific post ID to backfill
        full_text_only: If True, only find posts missing full_text (ignore other metadata)

    Returns:
        List of post records with missing metadata
    """
    base_select = """
        SELECT
            id,
            blog_id,
            title,
            url,
            summary,
            reading_time,
            discovered_date,
            technical_density,
            full_text,
            (SELECT COUNT(*) FROM post_topics WHERE post_id = posts.id) as topic_count
        FROM posts
    """

    if post_id:
        cursor.execute(f"{base_select} WHERE id = %s", (post_id,))
    elif full_text_only:
        # Find all posts missing full_text
        cursor.execute(
            f"{base_select} WHERE full_text IS NULL OR full_text = '' ORDER BY discovered_date DESC"
        )
    elif hours:
        cursor.execute(
            f"""{base_select}
            WHERE discovered_date > NOW() - INTERVAL '%s hours'
            AND (summary IS NULL OR summary = '' OR technical_density = -1)
            ORDER BY discovered_date DESC
            """,
            (hours,),
        )
    else:
        cursor.execute(
            f"""{base_select}
            WHERE (summary IS NULL OR summary = '' OR technical_density = -1)
            ORDER BY discovered_date DESC
            LIMIT 50
            """
        )

    return cursor.fetchall()


def get_existing_topics(cursor):
    """Get all existing topic names from database."""
    cursor.execute("SELECT name FROM topics ORDER BY name")
    return [row["name"] for row in cursor.fetchall()]


def backfill_full_text(cursor, post, dry_run: bool = False):
    """
    Fetch and save full_text for a single post.

    Args:
        cursor: Database cursor
        post: Post record from database
        dry_run: If True, don't actually update database

    Returns:
        dict with status and info
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing post {post['id']}: {post['title']}")
    print(f"  URL: {post['url']}")

    try:
        print("  Fetching post content...")
        response = fetch_with_retries(post["url"], retries=3, sleep=2)
        text_content = extract_post_text(response.text)

        if not text_content or len(text_content) < 100:
            print(f"  Warning: Extracted text too short ({len(text_content or '')} chars)")
            return {"status": "error", "error": "Text too short"}

        print(f"  Extracted {len(text_content)} characters of text")

        if not dry_run:
            cursor.execute(
                "UPDATE posts SET full_text = %s WHERE id = %s",
                (text_content, post["id"]),
            )
            print("  Database updated")

        return {"status": "success", "chars": len(text_content)}

    except Exception as e:
        print(f"  Error: {str(e)}")
        return {"status": "error", "error": str(e)}


def _backfill_full_text_worker(args: tuple) -> dict:
    """
    Worker function for parallel full_text backfill.

    Each worker gets its own database connection since psycopg2 connections
    are not thread/process safe.

    Args:
        args: Tuple of (post_dict, dry_run)

    Returns:
        dict with status, post_id, and error info if applicable
    """
    post, dry_run = args
    post_id = post["id"]
    title = post["title"][:50]

    try:
        response = fetch_with_retries(post["url"], retries=3, sleep=2)
        text_content = extract_post_text(response.text)

        if not text_content or len(text_content) < 100:
            return {
                "status": "error",
                "post_id": post_id,
                "title": title,
                "error": f"Text too short ({len(text_content or '')} chars)",
            }

        if not dry_run:
            # Each worker gets its own connection
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE posts SET full_text = %s WHERE id = %s",
                (text_content, post_id),
            )
            conn.commit()
            conn.close()

        return {
            "status": "success",
            "post_id": post_id,
            "title": title,
            "chars": len(text_content),
        }

    except Exception as e:
        return {
            "status": "error",
            "post_id": post_id,
            "title": title,
            "error": str(e),
        }


def backfill_post(cursor, post, existing_topics: list[str], dry_run: bool = False):
    """
    Re-extract and update metadata for a single post.

    Args:
        cursor: Database cursor
        post: Post record from database
        existing_topics: List of existing topic names
        dry_run: If True, don't actually update database

    Returns:
        dict with status and metadata
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing post {post['id']}: {post['title']}")
    print(f"  URL: {post['url']}")
    print(
        f"  Current state: summary={'✓' if post['summary'] else '✗'}, topics={post['topic_count']}"
    )

    try:
        # Use existing full_text if available, otherwise fetch
        if post.get("full_text"):
            print("  Using cached full_text from database...")
            text_content = post["full_text"]
            print(f"  Loaded {len(text_content)} characters of text")
        else:
            print("  Fetching post content...")
            response = fetch_with_retries(post["url"], retries=3, sleep=2)
            text_content = extract_post_text(response.text)

            if not text_content or len(text_content) < 100:
                print(f"  ⚠️  Warning: Extracted text too short ({len(text_content or '')} chars)")
                return {"status": "error", "error": "Text too short"}

            print(f"  Extracted {len(text_content)} characters of text")

        # Check what needs to be extracted (idempotent - only extract missing fields)
        needs_summary = not post.get("summary") or post.get("summary", "").strip() == ""
        needs_technical_density = post.get("technical_density") == -1
        needs_topics = post.get("topic_count", 0) == 0

        summary = post.get("summary")
        technical_density = post.get("technical_density", -1)
        matched_topics = []

        # Extract summary/technical_density only if needed
        if needs_summary or needs_technical_density:
            print(
                f"  Extracting {'summary and ' if needs_summary else ''}technical_density with LLM..."
            )
            summary_data = extract_summary(text_content)

            if needs_summary:
                new_summary = summary_data.get("summary")
                if not new_summary:
                    print("  ⚠️  Warning: Summary extraction returned empty")
                    return {"status": "partial", "summary": None, "topics": []}
                summary = new_summary
                print(f"  ✓ Summary: {summary[:80]}...")
            else:
                print(
                    f"  ℹ️  Summary already exists, keeping: {summary[:80] if summary else 'N/A'}..."
                )

            if needs_technical_density:
                technical_density = summary_data.get("technical_density", 2)
                print(f"  ✓ Technical density: {technical_density}")
            else:
                print(f"  ℹ️  Technical density already set: {technical_density}")
        else:
            print("  ℹ️  Summary and technical_density already set, skipping LLM extraction")

        # Extract topics if missing
        if needs_topics:
            print("  Extracting topics with LLM...")
            topics_data = extract_topics(text_content, existing_topics)
            matched_topics = topics_data.get("matched_topics", [])
            print(f"  ✓ Topics: {matched_topics}")
        else:
            print("  ℹ️  Topics already set, skipping topic extraction")

        if not dry_run:
            # Build dynamic UPDATE statement for only the fields that need updating
            updates = []
            params = []

            # Always update full_text if we fetched it (not from cache)
            if not post.get("full_text"):
                updates.append("full_text = %s")
                params.append(text_content)

            # Update summary if it was missing
            if needs_summary and summary:
                updates.append("summary = %s")
                params.append(summary)

            # Update technical_density if it was -1
            if needs_technical_density:
                updates.append("technical_density = %s")
                params.append(technical_density)

            # Execute UPDATE if there are any fields to update
            if updates:
                params.append(post["id"])
                update_sql = f"UPDATE posts SET {', '.join(updates)} WHERE id = %s"
                cursor.execute(update_sql, tuple(params))
                print(f"  📝 Updated fields: {', '.join([u.split(' = ')[0] for u in updates])}")
            else:
                print("  ℹ️  No fields needed updating (idempotent)")

            # Add topics only if they were extracted (idempotent)
            if needs_topics and matched_topics:
                psycopg2.extras.execute_values(
                    cursor,
                    "INSERT INTO topics (name) VALUES %s ON CONFLICT (name) DO NOTHING",
                    [(topic,) for topic in matched_topics],
                )

                # Link topics to post
                # First, get topic IDs
                cursor.execute(
                    "SELECT id, name FROM topics WHERE name = ANY(%s)", (matched_topics,)
                )
                topic_map = {row["name"]: row["id"] for row in cursor.fetchall()}

                # Delete existing post_topics (in case there are stale ones)
                cursor.execute("DELETE FROM post_topics WHERE post_id = %s", (post["id"],))

                # Insert new post_topics
                psycopg2.extras.execute_values(
                    cursor,
                    "INSERT INTO post_topics (post_id, topic_id) VALUES %s",
                    [
                        (post["id"], topic_map[topic])
                        for topic in matched_topics
                        if topic in topic_map
                    ],
                )
                print(f"  🏷️  Added {len(matched_topics)} topics")
            elif needs_topics:
                print("  ⚠️  No topics extracted")

            print("  ✅ Database updated")
        else:
            print("  [DRY RUN] Would update database")

        return {
            "status": "success",
            "summary": summary,
            "technical_density": technical_density,
            "topics": matched_topics,
        }

    except Exception as e:
        print(f"  ❌ Error: {str(e)}")
        import traceback

        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(
        description="Backfill missing summaries and topics for posts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hours", type=int, help="Only backfill posts discovered in last N hours")
    parser.add_argument("--post-id", type=int, help="Backfill specific post by ID")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be updated without making changes"
    )
    parser.add_argument(
        "--full-text-only",
        action="store_true",
        help="Only backfill full_text for posts missing it (skips summary/topics extraction)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel workers (default: {DEFAULT_WORKERS})",
    )

    args = parser.parse_args()

    if not args.hours and not args.post_id and not args.full_text_only:
        parser.error("Must specify either --hours, --post-id, or --full-text-only")

    print("=" * 80)
    print("BACKFILL SCRIPT: Re-extract summaries and topics")
    print("=" * 80)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No changes will be made to database\n")

    # Connect to database
    conn = get_connection()
    cursor = conn.cursor()

    # Get posts needing backfill
    print("\nFinding posts needing backfill...")
    posts = get_posts_needing_backfill(
        cursor,
        hours=args.hours,
        post_id=args.post_id,
        full_text_only=args.full_text_only,
    )

    if not posts:
        print("No posts found needing backfill!")
        conn.close()
        return

    print(f"Found {len(posts)} post(s) to backfill:\n")
    for post in posts:
        print(f"  {post['id']:3d}. {post['title'][:60]}")
        if args.full_text_only:
            print(f"       Discovered: {post['discovered_date']}")
        else:
            print(
                f"       Discovered: {post['discovered_date']}, Summary: {'Y' if post['summary'] else 'N'}, Topics: {post['topic_count']}"
            )

    # Confirm
    if not args.dry_run:
        response = input(f"\nProceed with backfilling {len(posts)} post(s)? [y/N] ")
        if response.lower() != "y":
            print("Aborted.")
            conn.close()
            return

    # Process each post
    results = {"success": 0, "partial": 0, "error": 0}

    if args.full_text_only:
        # Full-text only mode: parallel processing
        num_workers = min(args.workers, len(posts))
        print(f"\nProcessing with {num_workers} parallel workers...")

        # Prepare work items - convert RealDictRow to regular dict for pickling
        work_items = [(dict(post), args.dry_run) for post in posts]

        with mp.Pool(processes=num_workers) as pool:
            for i, result in enumerate(
                pool.imap_unordered(_backfill_full_text_worker, work_items), 1
            ):
                status = result["status"]
                results[status] += 1

                if status == "success":
                    print(f"[{i}/{len(posts)}] OK: {result['title']} ({result['chars']} chars)")
                else:
                    print(
                        f"[{i}/{len(posts)}] FAIL: {result['title']} - {result.get('error', 'Unknown')}"
                    )
    else:
        # Full metadata backfill mode
        existing_topics = get_existing_topics(cursor)
        print(f"\nLoaded {len(existing_topics)} existing topics from database")

        for i, post in enumerate(posts, 1):
            print(f"\n[{i}/{len(posts)}] ", end="")
            result = backfill_post(cursor, post, existing_topics, dry_run=args.dry_run)
            results[result["status"]] += 1

            if not args.dry_run and result["status"] in ["success", "partial"]:
                conn.commit()

    # Summary
    print("\n" + "=" * 80)
    print("BACKFILL SUMMARY")
    print("=" * 80)
    print(f"Total posts: {len(posts)}")
    print(f"✅ Success: {results['success']}")
    print(f"⚠️  Partial: {results['partial']}")
    print(f"❌ Error: {results['error']}")
    print(f"\nCompleted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No changes were made to database")

    conn.close()


if __name__ == "__main__":
    main()
