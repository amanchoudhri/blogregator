import os

import psycopg2
import psycopg2.extras

from blogregator.utils import utcnow

def get_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required!")
    conn = psycopg2.connect(
            database_url,
            cursor_factory=psycopg2.extras.RealDictCursor 
            )
    return conn

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