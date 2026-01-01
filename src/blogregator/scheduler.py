"""Scheduler for periodic blog checks and newsletter sending."""

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from blogregator.alerts import alert_check_failed, alert_newsletter_failed
from blogregator.config import get_config
from blogregator.core import run_blog_check, send_newsletter_if_needed

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None
_last_check_time: datetime | None = None
_last_check_result: dict | None = None
_next_check_time: datetime | None = None


def get_scheduler_status() -> dict:
    """Get current scheduler status."""
    return {
        "last_check_time": _last_check_time.isoformat() if _last_check_time else None,
        "last_check_result": _last_check_result,
        "next_check_time": _next_check_time.isoformat() if _next_check_time else None,
        "scheduler_running": _scheduler is not None and _scheduler.running,
    }


@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=60, max=1800),  # 1 min to 30 min
    reraise=True,
)
def scheduled_blog_check_with_retry():
    """Run blog check with retry logic for transient failures."""
    global _last_check_time, _last_check_result

    config = get_config()
    logger.info("Starting scheduled blog check")

    try:
        # Run blog check
        result = run_blog_check(max_workers=config.max_workers)

        _last_check_time = datetime.utcnow()
        _last_check_result = {
            "success": result.success,
            "blogs_checked": result.blogs_checked,
            "new_posts_found": result.total_metrics.new_posts_found,
            "posts_added": result.total_metrics.full_success + result.total_metrics.partial_success,
            "blog_parse_errors": result.blog_parse_errors,
            "error": result.error_message,
        }

        if not result.success:
            logger.error("Scheduled blog check failed", extra={"error": result.error_message})
            alert_check_failed(
                error=Exception(result.error_message or "Unknown error"),
                retry_count=0,
                next_action="Will retry on next scheduled run",
            )
            return

        logger.info(
            "Scheduled blog check completed successfully",
            extra={
                "blogs_checked": result.blogs_checked,
                "new_posts_found": result.total_metrics.new_posts_found,
                "posts_added": result.total_metrics.full_success
                + result.total_metrics.partial_success,
            },
        )

        # Send newsletter if there are new posts
        if result.total_metrics.new_posts_found > 0:
            logger.info("New posts found, sending newsletter")
            success, n_posts, error = send_newsletter_if_needed(
                hour_window=config.newsletter_window_hours
            )

            if not success:
                logger.error("Newsletter send failed", extra={"error": error})
                alert_newsletter_failed(Exception(error or "Unknown error"))
            else:
                logger.info("Newsletter sent successfully", extra={"posts_count": n_posts})

    except Exception as e:
        logger.error(
            "Scheduled blog check failed with exception", extra={"error": str(e)}, exc_info=True
        )
        _last_check_time = datetime.utcnow()
        _last_check_result = {
            "success": False,
            "error": str(e),
        }
        raise


def scheduled_blog_check():
    """Wrapper for scheduled blog check that handles final failure after retries."""
    try:
        scheduled_blog_check_with_retry()
    except Exception as e:
        logger.critical(
            "Scheduled blog check failed after all retries",
            extra={"error": str(e)},
            exc_info=True,
        )
        alert_check_failed(
            error=e,
            retry_count=3,
            next_action="Waiting for next scheduled run",
        )


def send_daily_digest():
    """Send daily digest newsletter at scheduled time."""
    logger.info("Running daily digest newsletter send")

    try:
        # For daily digest, use a 24-hour window
        success, n_posts, error = send_newsletter_if_needed(hour_window=24)

        if not success:
            logger.error("Daily digest send failed", extra={"error": error})
            alert_newsletter_failed(Exception(error or "Unknown error"))
        elif n_posts > 0:
            logger.info("Daily digest sent successfully", extra={"posts_count": n_posts})
        else:
            logger.info("No posts to include in daily digest")

    except Exception as e:
        logger.error("Daily digest failed with exception", extra={"error": str(e)}, exc_info=True)
        alert_newsletter_failed(e)


def start_scheduler():
    """Start the APScheduler for periodic tasks."""
    global _scheduler, _next_check_time

    config = get_config()

    _scheduler = AsyncIOScheduler()

    # Schedule blog checks every N hours
    _scheduler.add_job(
        scheduled_blog_check,
        trigger=IntervalTrigger(hours=config.check_interval_hours),
        id="blog_check",
        name="Check blogs for new posts",
        replace_existing=True,
    )

    # Schedule daily digest at midnight UTC
    _scheduler.add_job(
        send_daily_digest,
        trigger="cron",
        hour=0,
        minute=0,
        id="daily_digest",
        name="Send daily digest newsletter",
        replace_existing=True,
    )

    _scheduler.start()

    # Calculate next check time
    job = _scheduler.get_job("blog_check")
    if job and job.next_run_time:
        _next_check_time = job.next_run_time.replace(tzinfo=None)

    logger.info(
        "Scheduler started",
        extra={
            "check_interval_hours": config.check_interval_hours,
            "next_check_time": _next_check_time.isoformat() if _next_check_time else None,
        },
    )


def stop_scheduler():
    """Stop the scheduler gracefully."""
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.info("Stopping scheduler...")
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("Scheduler stopped")


def get_scheduler() -> AsyncIOScheduler | None:
    """Get the global scheduler instance."""
    return _scheduler
