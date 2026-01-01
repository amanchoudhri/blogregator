-- Blogregator Database Schema
-- This schema reflects the actual production database structure

-- Blogs table - stores blog information and scraping configuration
CREATE TABLE IF NOT EXISTS blogs (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE,
    last_checked TIMESTAMPTZ,
    scraping_schema TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    scraping_successful BOOLEAN NOT NULL DEFAULT false,
    last_modified_at TIMESTAMPTZ DEFAULT NOW(),
    last_modified_by INTEGER,
    proposed_schema TEXT,
    refinement_attempts INTEGER DEFAULT 0
);

-- Posts table - stores discovered blog posts
CREATE TABLE IF NOT EXISTS posts (
    id SERIAL PRIMARY KEY,
    blog_id INTEGER REFERENCES blogs(id),
    title TEXT,
    url TEXT UNIQUE,
    reading_time INTEGER,
    summary TEXT,
    publication_date TIMESTAMPTZ,
    discovered_date TIMESTAMPTZ DEFAULT NOW(),
    technical_density INTEGER DEFAULT -1,  -- -1 = not set, 1 = low, 2 = medium, 3 = high
    full_text TEXT  -- Full extracted text content from the post
);

-- Topics table - stores topic/tag names
CREATE TABLE IF NOT EXISTS topics (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL
);

-- Post_topics junction table - many-to-many relationship between posts and topics
CREATE TABLE IF NOT EXISTS post_topics (
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, topic_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_posts_blog_id ON posts(blog_id);
CREATE INDEX IF NOT EXISTS idx_posts_discovered_date ON posts(discovered_date);

-- Note: The production database also has these tables/constraints that are not used by this codebase:
-- - users table (referenced by blogs.last_modified_by)
-- - blog_users table (many-to-many between blogs and users)
-- - tickets table (related to blogs)
-- These are likely from a larger system and are not created by this schema file.
