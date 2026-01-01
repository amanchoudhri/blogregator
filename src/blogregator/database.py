import logging
import os

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required!")
    conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_database(sql_file: str = "sql/schema.sql"):
    """Initialize the database by creating tables from a schema file."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        with open(sql_file) as f:
            cursor.execute(f.read())
        conn.commit()
        logger.info("Database initialized successfully", extra={"sql_file": sql_file})
    except Exception as e:
        logger.error("Failed to initialize database", extra={"error": str(e)}, exc_info=True)
        raise e
    finally:
        conn.close()


def log_error(cursor, blog_id: int, error_type: str, message: str):
    """
    Log an error to Python logging system.

    Note: The error_log table doesn't exist in the production database,
    so we use Python's logging instead of database logging.
    """
    logger.error(
        "Blog processing error",
        extra={
            "blog_id": blog_id,
            "error_type": error_type,
            "error_message": message,
        },
    )
