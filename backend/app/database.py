import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .utils import utcnow

def get_connection() -> psycopg.Connection[dict[str, Any]]:
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required!")
    # known pyright typing incompatibility:
    # https://github.com/psycopg/psycopg/issues/865
    conn = psycopg.connect(database_url, row_factory=dict_row) # type: ignore
    return conn # type: ignore

def init_database(sql_file: str = "sql/schema.sql"):
    """Initialize the database by creating tables from a schema file."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        with open(sql_file, 'r') as f:
            cursor.execute(f.read())
        conn.commit()
    except Exception as e:
        raise e
    finally:
        conn.close()
        

def log_error(cursor, blog_id: int, error_type: str, message: str):
    """Insert an error log entry."""
    cursor.execute(
        "INSERT INTO error_log (blog_id, timestamp, error_type, message) VALUES (%s, %s, %s, %s)",
        (blog_id, utcnow().isoformat(), error_type, message)
    )
