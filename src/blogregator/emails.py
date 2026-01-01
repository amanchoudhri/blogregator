import os
import smtplib
from collections.abc import Mapping
from email.mime.text import MIMEText
from typing import Any

from blogregator.config import get_config
from blogregator.database import get_connection


def get_new_posts(hour_window: int = 8) -> list[Mapping[str, Any]]:
    """Get new posts discovered in the last hour_window hours."""
    config = get_config()
    conn = get_connection()
    cursor = conn.cursor()
    # find all posts discovered in the last hour_window hours
    # exclude posts from newly added blogs (discovered within grace period after blog creation)
    cursor.execute(
        """
        SELECT
            p.id,
            p.title,
            p.url,
            p.publication_date,
            p.reading_time,
            p.summary,
            b.url as blog_name,
            STRING_AGG(t.name, ', ' ORDER BY t.name) as topics
        FROM posts p
        LEFT JOIN post_topics tp ON p.id = tp.post_id
        LEFT JOIN topics t ON t.id = tp.topic_id
        LEFT JOIN blogs b ON b.id = p.blog_id
        WHERE discovered_date > NOW() - INTERVAL %s
          AND discovered_date >= b.created_at + INTERVAL %s
        GROUP BY p.id, b.id, b.url;
        """,
        (f"'{hour_window} hour'", f"'{config.new_blog_grace_period_hours} hour'"),
    )
    posts = cursor.fetchall()
    conn.close()
    return posts  # type: ignore


def notify(hour_window: int = 8) -> int:
    """
    Send an email with new posts discovered in the last hour_window hours.

    Returns:
        int: the number of posts discovered and sent in the email.
    """
    posts = get_new_posts(hour_window)
    if not posts:
        return 0

    # Get environment variables
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", ""))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    email_to = os.getenv("EMAIL_TO", "")

    if not all([smtp_host, smtp_port, smtp_user, smtp_password, email_to]):
        raise ValueError("Missing environment variables")

    html_body = newsletter_html([post_html(p) for p in posts])

    n_posts = len(posts)
    msg = MIMEText(html_body, "html")
    msg["Subject"] = f"📚 {n_posts} new blog post" + ("s" if n_posts > 1 else "")
    msg["From"] = f"Blogregator <{smtp_user}>"
    msg["To"] = email_to

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
            return n_posts
    except Exception as e:
        raise Exception(f"Failed to send email: {e}") from e


def post_html(post: Mapping[str, Any], max_n_topics: int = 3):
    """Generate HTML for a single post in the newsletter."""
    topic_badges = ""
    # Handle topics properly
    topics_str = post.get("topics", "") or ""
    topics = [t.strip() for t in topics_str.split(",") if t.strip()] if topics_str else []
    if topics:
        badges = []
        for topic in topics[:max_n_topics]:
            badges.append(
                f'<span style="color: #0066cc; font-size: 13px; margin-right: 12px; display: inline-block">{topic}</span>'
            )
        topic_badges = "".join(badges)

    # Format reading time and date
    reading_time = f"{post.get('reading_time', '?')} min read"
    pub_date_display = ""
    if post.get("publication_date"):
        try:
            if hasattr(post["publication_date"], "strftime"):
                pub_date = post["publication_date"].strftime("%Y-%m-%d")
            else:
                pub_date = str(post["publication_date"])[:10]  # Take first 10 chars if string
            pub_date_display = f" • {pub_date}"
        except (ValueError, AttributeError, TypeError):
            pass
    return f"""
    <table style="width: 100%; margin-bottom: 20px; border-collapse: collapse;">
        <tr>
            <td style="padding: 0;">
                <h2 style="margin: 0 0 6px 0; color: #1a1a1a; font-size: 20px; font-weight: 600; line-height: 1.3;">
                    <a href="{post["url"]}" style="text-decoration: none; color: #1a1a1a;">{post["title"]}</a>
                </h2>
                <div style="color: #666; font-size: 14px; margin-bottom: 12px; font-weight: 500;">
                    {post.get("blog_name", "Unknown Blog")} • {reading_time}{pub_date_display}
                </div>
                {f'<p style="color: #444; margin: 0 0 12px 0; line-height: 1.5; font-size: 15px;">{post.get("summary", "")}</p>' if post.get("summary") else ""}
                {f'<div style="margin: 0;">{topic_badges}</div>' if topic_badges else ""}
            </td>
        </tr>
        <tr>
            <td style="padding: 16px 0 0 0;">
                <hr style="border: none; border-top: 1px solid #e8e8e8; margin: 0;">
            </td>
        </tr>
    </table>
    """


def newsletter_html(post_htmls: list[str]):
    """Generate HTML for a newsletter."""
    n_posts = len(post_htmls)
    posts_display = "\n".join(post_htmls)
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; background: #ffffff;">
        <table style="max-width: 680px; margin: 0 auto; width: 100%;">
            <tr>
                <td style="padding: 16px 24px 16px;">
                    <h2 style="color: #1a1a1a; margin: 0 0 6px 0; font-size: 26px; font-weight: 700;">📚 New Blog Posts</h2>
                    <p style="color: #666; margin: 0 0 20px 0; font-size: 16px;">Found {n_posts} interesting {"post" if n_posts == 1 else "posts"} for you:</p>

                    {posts_display}

                    <p style="color: #999; font-size: 13px; margin: -12px 0 0 0; padding-top: 16px;">
                        Generated by your blog monitoring system • <a href="https://github.com/amanchoudhri/blogregator">View on GitHub</a>
                    </p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """
