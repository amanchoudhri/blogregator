-- Create the main tables
CREATE TABLE IF NOT EXISTS blogs (
    id SERIAL PRIMARY KEY,
    name TEXT,
    url TEXT UNIQUE,
    last_checked TIMESTAMPTZ,
    scraping_schema TEXT,
    status TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS posts (
    id SERIAL PRIMARY KEY,
    blog_id INTEGER REFERENCES blogs(id),
    title TEXT,
    url TEXT UNIQUE,
    topics TEXT,
    reading_time INTEGER,
    summary TEXT,
    publication_date TIMESTAMPTZ,
    discovered_date TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    blog_id INTEGER REFERENCES blogs(id),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    error_type TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_blog_id ON posts(blog_id);
CREATE INDEX IF NOT EXISTS idx_posts_discovered_date ON posts(discovered_date);
CREATE INDEX IF NOT EXISTS idx_blogs_status ON blogs(status);
CREATE INDEX IF NOT EXISTS idx_error_log_blog_id ON error_log(blog_id);