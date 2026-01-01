"""Alert email system for critical errors and failures."""

import logging
import smtplib
import traceback
from email.mime.text import MIMEText

from blogregator.config import get_config

logger = logging.getLogger(__name__)


def send_alert_email(
    subject: str,
    error_type: str,
    error_message: str,
    context: dict | None = None,
    include_traceback: bool = True,
) -> bool:
    """
    Send an alert email for critical errors.

    Args:
        subject: Email subject line
        error_type: Type of error (e.g., "scheduled_check_failed", "database_error")
        error_message: Error message to include
        context: Additional context dictionary
        include_traceback: Whether to include full traceback

    Returns:
        True if email sent successfully, False otherwise
    """
    config = get_config()

    try:
        # Build email body
        body_parts = [
            f"Blogregator Alert: {error_type}",
            "",
            "Error Message:",
            error_message,
            "",
        ]

        if context:
            body_parts.append("Context:")
            for key, value in context.items():
                body_parts.append(f"  {key}: {value}")
            body_parts.append("")

        if include_traceback:
            body_parts.append("Traceback:")
            body_parts.append(traceback.format_exc())
            body_parts.append("")

        body_parts.extend(
            [
                "---",
                "This is an automated alert from your Blogregator server.",
                "Please investigate the issue as soon as possible.",
            ]
        )

        body = "\n".join(body_parts)

        # Create and send email
        msg = MIMEText(body, "plain")
        msg["Subject"] = f"🚨 Blogregator Alert: {subject}"
        msg["From"] = f"Blogregator Alerts <{config.smtp_user}>"
        msg["To"] = config.alert_email

        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.starttls()
            server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)

        logger.info(
            "Alert email sent successfully", extra={"subject": subject, "error_type": error_type}
        )
        return True

    except Exception as e:
        logger.error(
            "Failed to send alert email",
            extra={"error": str(e), "original_error_type": error_type},
            exc_info=True,
        )
        return False


def alert_check_failed(error: Exception, retry_count: int, next_action: str):
    """Alert when scheduled blog check fails after retries."""
    send_alert_email(
        subject="Scheduled Check Failed",
        error_type="scheduled_check_failed",
        error_message=str(error),
        context={
            "retry_count": retry_count,
            "next_action": next_action,
        },
    )


def alert_database_error(error: Exception, operation: str):
    """Alert when database connection or operation fails."""
    send_alert_email(
        subject="Database Error",
        error_type="database_error",
        error_message=str(error),
        context={
            "operation": operation,
        },
    )


def alert_newsletter_failed(error: Exception):
    """Alert when newsletter sending fails."""
    send_alert_email(
        subject="Newsletter Send Failed",
        error_type="newsletter_failed",
        error_message=str(error),
    )


def alert_server_health_check_failed(details: str):
    """Alert when server health check fails."""
    send_alert_email(
        subject="Server Health Check Failed",
        error_type="health_check_failed",
        error_message=details,
        include_traceback=False,
    )
