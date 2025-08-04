-- Enable the case-insensitive text data type
CREATE EXTENSION IF NOT EXISTS citext;

-- Create the main tables
CREATE TABLE IF NOT EXISTS blogs (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE,
    last_checked TIMESTAMPTZ,
    scraping_schema TEXT,
    proposed_schema TEXT DEFAULT '',
    scraping_successful BOOL DEFAULT FALSE,
    refinement_attempts INTEGER DEFAULT 0,
    last_modified_by INTEGER REFERENCES users(id),
    last_modified_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
);
CREATE TABLE IF NOT EXISTS posts (
    id SERIAL PRIMARY KEY,
    blog_id INTEGER REFERENCES blogs(id),
    title TEXT,
    url TEXT UNIQUE,
    reading_time INTEGER,
    summary TEXT,
    publication_date TIMESTAMPTZ,
    discovered_date TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS topics (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS post_topics (
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, topic_id)
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email CITEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    jwt_version INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blog_users (
    blog_id INTEGER REFERENCES blogs(id) ON DELETE RESTRICT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (blog_id, user_id)
);
-- Speed up `blogs for user_id` queries
CREATE INDEX IF NOT EXISTS idx_blog_users_user_id ON blog_users(user_id);

CREATE TABLE IF NOT EXISTS otps (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    otp_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid BOOL DEFAULT FALSE,
    PRIMARY KEY (user_id, created_at)
);

-- Support requests for nonfunctional blog schemas
CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    blog_id INTEGER REFERENCES blogs(id),
    opened_by INTEGER REFERENCES users(id),
    opened_at TIMESTAMPTZ DEFAULT NOW(),
    message TEXT,
    resolved BOOL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ
)
CREATE INDEX IF NOT EXISTS idx_tickets_resolution_time ON tickets(resolved, opened_at);



CREATE INDEX IF NOT EXISTS idx_posts_blog_id ON posts(blog_id);
CREATE INDEX IF NOT EXISTS idx_posts_discovered_date ON posts(discovered_date);
CREATE INDEX IF NOT EXISTS idx_error_log_blog_id ON error_log(blog_id);

