import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration for the blogregator server."""

    # Database
    database_url: str

    # LLM
    gemini_api_key: str

    # Email (newsletters)
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_to: str

    # Alert email
    alert_email: str

    # Server settings
    check_interval_hours: int = 6
    newsletter_window_hours: int = 6
    new_blog_grace_period_hours: int = 1
    log_level: str = "INFO"
    max_workers: int = 8

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        # Required variables
        database_url = os.getenv("DATABASE_URL")
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        smtp_host = os.getenv("SMTP_HOST")
        smtp_port_str = os.getenv("SMTP_PORT")
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASSWORD")
        email_to = os.getenv("EMAIL_TO")
        alert_email = os.getenv("ALERT_EMAIL", "amanchoudhri@gmail.com")

        # Validate required variables
        missing = []
        if not database_url:
            missing.append("DATABASE_URL")
        if not gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not smtp_host:
            missing.append("SMTP_HOST")
        if not smtp_port_str:
            missing.append("SMTP_PORT")
        if not smtp_user:
            missing.append("SMTP_USER")
        if not smtp_password:
            missing.append("SMTP_PASSWORD")
        if not email_to:
            missing.append("EMAIL_TO")

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        # Type narrowing: all variables are guaranteed to be non-None after validation
        assert database_url is not None
        assert gemini_api_key is not None
        assert smtp_host is not None
        assert smtp_port_str is not None
        assert smtp_user is not None
        assert smtp_password is not None
        assert email_to is not None

        # Parse port
        try:
            smtp_port = int(smtp_port_str)
        except ValueError as e:
            raise ValueError(f"SMTP_PORT must be an integer, got: {smtp_port_str}") from e

        # Optional variables with defaults
        check_interval_hours = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
        newsletter_window_hours = int(os.getenv("NEWSLETTER_WINDOW_HOURS", "6"))
        new_blog_grace_period_hours = int(os.getenv("NEW_BLOG_GRACE_PERIOD_HOURS", "1"))
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        max_workers = int(os.getenv("MAX_WORKERS", "8"))

        return cls(
            database_url=database_url,
            gemini_api_key=gemini_api_key,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            email_to=email_to,
            alert_email=alert_email,
            check_interval_hours=check_interval_hours,
            newsletter_window_hours=newsletter_window_hours,
            new_blog_grace_period_hours=new_blog_grace_period_hours,
            log_level=log_level,
            max_workers=max_workers,
        )


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def set_config(config: Config):
    """Set the global configuration instance (useful for testing)."""
    global _config
    _config = config
