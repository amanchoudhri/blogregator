import sqlite3

from pathlib import Path
from sqlite3 import Cursor

DB_PATH = Path(__file__).parent / "blogregator.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Create tables
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS blogs (
        id INTEGER PRIMARY KEY,
        name TEXT,
        url TEXT UNIQUE,
        last_checked DATETIME,
        scraping_schema TEXT,
        status TEXT
    );
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY,
        blog_id INTEGER,
        title TEXT,
        url TEXT UNIQUE,
        content TEXT,
        html_content TEXT,
        publication_date DATETIME,
        discovered_date DATETIME,
        FOREIGN KEY(blog_id) REFERENCES blogs(id)
    );
    CREATE TABLE IF NOT EXISTS metadata (
        post_id INTEGER PRIMARY KEY,
        topics TEXT,
        reading_time INTEGER,
        summary TEXT,
        extraction_date DATETIME,
        FOREIGN KEY(post_id) REFERENCES posts(id)
    );
    CREATE TABLE IF NOT EXISTS error_log (
        id INTEGER PRIMARY KEY,
        blog_id INTEGER,
        timestamp DATETIME,
        error_type TEXT,
        message TEXT,
        FOREIGN KEY(blog_id) REFERENCES blogs(id)
    );
    """)
    conn.commit()
    conn.close()
