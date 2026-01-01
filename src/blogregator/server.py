"""FastAPI server for Blogregator with scheduled background tasks."""

import json
import logging
import logging.handlers
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pythonjsonlogger.json
import uvicorn
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from blogregator.blog import generate_schema, get_domain_name
from blogregator.config import get_config
from blogregator.core import run_blog_check, send_newsletter_if_needed
from blogregator.database import get_connection
from blogregator.llm import generate_json_from_llm
from blogregator.parser import parse_post_list
from blogregator.prompts import CORRECT_SCHEMA
from blogregator.scheduler import (
    get_scheduler_status,
    start_scheduler,
    stop_scheduler,
)
from blogregator.utils import fetch_with_retries


# Configure structured logging
def setup_logging():
    """Configure structured JSON logging with rotation."""
    config = get_config()

    # Create logs directory
    log_dir = Path("/app/logs" if os.path.exists("/app/logs") else "logs")
    log_dir.mkdir(exist_ok=True)

    # Create formatter
    log_format = "%(asctime)s %(levelname)s %(name)s %(funcName)s %(message)s"
    formatter = pythonjsonlogger.json.JsonFormatter(log_format)

    # File handler with rotation (10 files × 1MB = 10MB max)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "blogregator.log",
        maxBytes=1024 * 1024,  # 1MB
        backupCount=10,
    )
    file_handler.setFormatter(formatter)

    # Console handler for container logs
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return root_logger


logger = logging.getLogger(__name__)


# Pydantic models for request validation
class SchemaGenerationRequest(BaseModel):
    url: str = Field(..., description="Blog URL to generate schema for")


class AddBlogRequest(BaseModel):
    url: str = Field(..., description="Blog URL")
    name: str | None = Field(None, description="Blog name (auto-generated if not provided)")
    scraping_schema: dict = Field(..., description="Scraping schema for extracting posts")
    validate_schema: bool = Field(True, description="Validate schema before saving")


class RefineSchemaRequest(BaseModel):
    url: str = Field(..., description="Blog URL")
    previous_schema: dict = Field(..., description="Previous schema to refine")
    feedback: str = Field(..., description="User feedback on what went wrong")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Starting Blogregator server...")

    try:
        # Validate configuration
        config = get_config()
        logger.info(
            "Configuration loaded",
            extra={
                "check_interval_hours": config.check_interval_hours,
                "newsletter_window_hours": config.newsletter_window_hours,
                "log_level": config.log_level,
            },
        )

        # Test database connection
        try:
            conn = get_connection()
            conn.close()
            logger.info("Database connection successful")
        except Exception as e:
            logger.error("Database connection failed", extra={"error": str(e)})
            raise

        # Start scheduler
        start_scheduler()
        logger.info("Scheduler started successfully")

    except Exception as e:
        logger.critical("Startup failed", extra={"error": str(e)}, exc_info=True)
        raise

    yield

    # Shutdown
    logger.info("Shutting down Blogregator server...")
    stop_scheduler()
    logger.info("Server shut down successfully")


# Create FastAPI app
app = FastAPI(
    title="Blogregator Server",
    description="Automated blog monitoring and newsletter system",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Display a simple status dashboard."""
    config = get_config()
    status = get_scheduler_status()

    # Get recent stats from database
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as count FROM blogs WHERE scraping_successful = true")
        active_blogs = cursor.fetchone()["count"]  # type: ignore

        cursor.execute(
            "SELECT COUNT(*) as count FROM posts WHERE discovered_date > NOW() - INTERVAL '24 hours'"
        )
        posts_24h = cursor.fetchone()["count"]  # type: ignore

        cursor.execute(
            "SELECT COUNT(*) as count FROM posts WHERE discovered_date > NOW() - INTERVAL '7 days'"
        )
        posts_7d = cursor.fetchone()["count"]  # type: ignore

        conn.close()
    except Exception as e:
        logger.error("Failed to fetch dashboard stats", extra={"error": str(e)})
        active_blogs = posts_24h = posts_7d = "Error"

    # Format times
    last_check = status["last_check_time"] or "Never"
    next_check = status["next_check_time"] or "Not scheduled"

    # Last check result
    last_result = status["last_check_result"]
    if last_result:
        if last_result.get("success"):
            result_text = f"""
            <span style="color: #22c55e;">✓ Success</span><br>
            Blogs checked: {last_result.get("blogs_checked", 0)}<br>
            New posts found: {last_result.get("new_posts_found", 0)}<br>
            Posts added: {last_result.get("posts_added", 0)}
            """
        else:
            result_text = f"""
            <span style="color: #ef4444;">✗ Failed</span><br>
            Error: {last_result.get("error", "Unknown")}
            """
    else:
        result_text = '<span style="color: #64748b;">No checks yet</span>'

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Blogregator Dashboard</title>
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
                background: #0f172a;
                color: #e2e8f0;
                padding: 2rem;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            h1 {{
                font-size: 2.5rem;
                margin-bottom: 0.5rem;
                color: #f8fafc;
            }}
            .subtitle {{
                color: #94a3b8;
                margin-bottom: 2rem;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 1.5rem;
                margin-bottom: 2rem;
            }}
            .card {{
                background: #1e293b;
                border-radius: 12px;
                padding: 1.5rem;
                border: 1px solid #334155;
            }}
            .card h2 {{
                font-size: 1.25rem;
                margin-bottom: 1rem;
                color: #f1f5f9;
            }}
            .stat {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 0.75rem 0;
                border-bottom: 1px solid #334155;
            }}
            .stat:last-child {{
                border-bottom: none;
            }}
            .stat-label {{
                color: #94a3b8;
                font-size: 0.875rem;
            }}
            .stat-value {{
                font-size: 1.5rem;
                font-weight: 600;
                color: #f8fafc;
            }}
            .status-indicator {{
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin-right: 8px;
            }}
            .status-running {{
                background: #22c55e;
                box-shadow: 0 0 10px #22c55e;
            }}
            .status-stopped {{
                background: #ef4444;
            }}
            .actions {{
                display: flex;
                gap: 1rem;
                flex-wrap: wrap;
            }}
            .btn {{
                padding: 0.75rem 1.5rem;
                border: none;
                border-radius: 8px;
                font-size: 0.875rem;
                font-weight: 600;
                cursor: pointer;
                text-decoration: none;
                display: inline-block;
                transition: all 0.2s;
            }}
            .btn-primary {{
                background: #3b82f6;
                color: white;
            }}
            .btn-primary:hover {{
                background: #2563eb;
            }}
            .btn-secondary {{
                background: #475569;
                color: white;
            }}
            .btn-secondary:hover {{
                background: #334155;
            }}
            .footer {{
                margin-top: 3rem;
                padding-top: 2rem;
                border-top: 1px solid #334155;
                color: #64748b;
                font-size: 0.875rem;
                text-align: center;
            }}
            .footer a {{
                color: #3b82f6;
                text-decoration: none;
            }}
            .footer a:hover {{
                text-decoration: underline;
            }}
        </style>
        <script>
            async function triggerCheck() {{
                if (!confirm('Trigger a manual blog check now?')) return;
                try {{
                    const response = await fetch('/check', {{ method: 'POST' }});
                    const data = await response.json();
                    alert(data.message || 'Check started!');
                    setTimeout(() => location.reload(), 2000);
                }} catch (error) {{
                    alert('Failed to trigger check: ' + error.message);
                }}
            }}
            async function sendNewsletter() {{
                if (!confirm('Send newsletter now?')) return;
                try {{
                    const response = await fetch('/newsletter', {{ method: 'POST' }});
                    const data = await response.json();
                    alert(data.message || 'Newsletter sent!');
                }} catch (error) {{
                    alert('Failed to send newsletter: ' + error.message);
                }}
            }}

            async function loadLogs() {{
                try {{
                    const response = await fetch('/logs?lines=20');
                    const data = await response.json();
                    const container = document.getElementById('logs-container');

                    if (!data.logs || data.logs.length === 0) {{
                        container.innerHTML = '<div style="color: #94a3b8;">No logs available yet</div>';
                        return;
                    }}

                    const logHtml = data.logs.map(log => {{
                        const level = log.levelname || 'INFO';
                        const color = {{
                            'DEBUG': '#64748b',
                            'INFO': '#3b82f6',
                            'WARNING': '#f59e0b',
                            'ERROR': '#ef4444',
                            'CRITICAL': '#dc2626'
                        }}[level] || '#94a3b8';

                        const time = log.asctime || '';
                        const name = log.name || '';
                        const msg = log.message || '';

                        return `<div style="margin-bottom: 0.5rem; border-left: 3px solid ${{color}}; padding-left: 0.5rem;">
                            <span style="color: #64748b;">${{time}}</span>
                            <span style="color: ${{color}}; font-weight: bold; margin: 0 0.5rem;">[${{level}}]</span>
                            <span style="color: #94a3b8;">${{name}}</span>
                            <span style="color: #e2e8f0; margin-left: 0.5rem;">${{msg}}</span>
                        </div>`;
                    }}).join('');

                    container.innerHTML = logHtml;
                    container.scrollTop = container.scrollHeight; // Scroll to bottom
                }} catch (error) {{
                    console.error('Failed to load logs:', error);
                }}
            }}

            // Load logs on page load
            loadLogs();

            // Refresh logs every 10 seconds
            setInterval(loadLogs, 10000);

            // Auto-refresh page every 30 seconds
            setTimeout(() => location.reload(), 30000);
        </script>
    </head>
    <body>
        <div class="container">
            <h1>📚 Blogregator</h1>
            <p class="subtitle">Automated Blog Monitoring System</p>

            <div class="grid">
                <div class="card">
                    <h2>
                        <span class="status-indicator {"status-running" if status["scheduler_running"] else "status-stopped"}"></span>
                        Scheduler Status
                    </h2>
                    <div class="stat">
                        <span class="stat-label">Status</span>
                        <span class="stat-value">{"Running" if status["scheduler_running"] else "Stopped"}</span>
                    </div>
                    <div class="stat">
                        <span class="stat-label">Check Interval</span>
                        <span class="stat-value">{config.check_interval_hours}h</span>
                    </div>
                    <div class="stat">
                        <span class="stat-label">Last Check</span>
                        <span class="stat-value" style="font-size: 1rem;">{last_check}</span>
                    </div>
                    <div class="stat">
                        <span class="stat-label">Next Check</span>
                        <span class="stat-value" style="font-size: 1rem;">{next_check}</span>
                    </div>
                </div>

                <div class="card">
                    <h2>Last Check Result</h2>
                    <div style="padding: 1rem 0;">
                        {result_text}
                    </div>
                </div>

                <div class="card">
                    <h2>Statistics</h2>
                    <div class="stat">
                        <span class="stat-label">Active Blogs</span>
                        <span class="stat-value">{active_blogs}</span>
                    </div>
                    <div class="stat">
                        <span class="stat-label">Posts (24h)</span>
                        <span class="stat-value">{posts_24h}</span>
                    </div>
                    <div class="stat">
                        <span class="stat-label">Posts (7d)</span>
                        <span class="stat-value">{posts_7d}</span>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Actions</h2>
                <div class="actions">
                    <a href="/add-blog" class="btn btn-primary">
                        ➕ Add New Blog
                    </a>
                    <button class="btn btn-primary" onclick="triggerCheck()">
                        ▶ Trigger Check Now
                    </button>
                    <button class="btn btn-secondary" onclick="sendNewsletter()">
                        ✉ Send Newsletter
                    </button>
                    <a href="/status" class="btn btn-secondary">📊 JSON Status</a>
                    <a href="/blogs" class="btn btn-secondary">📝 View Blogs</a>
                    <a href="/posts/recent" class="btn btn-secondary">📰 Recent Posts</a>
                    <a href="/health" class="btn btn-secondary">❤ Health Check</a>
                </div>
            </div>

            <div class="card" style="grid-column: 1 / -1;">
                <h2>📜 Recent Logs <span style="font-size: 0.8rem; color: #94a3b8; font-weight: normal;">(Last 20 entries)</span></h2>
                <div id="logs-container" style="background: #0f172a; border-radius: 8px; padding: 1rem; max-height: 400px; overflow-y: auto; font-family: 'Monaco', 'Courier New', monospace; font-size: 0.75rem;">
                    <div style="color: #94a3b8;">Loading logs...</div>
                </div>
            </div>

            <div class="footer">
                <p>Blogregator v1.0.0 • Auto-refresh every 30s</p>
                <p><a href="https://github.com/amanchoudhri/blogregator">View on GitHub</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/add-blog", response_class=HTMLResponse)
async def add_blog_page():
    """Interactive page for adding a new blog with schema generation and refinement."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Add Blog - Blogregator</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
                background: #0f172a;
                color: #e2e8f0;
                padding: 2rem;
            }
            .container {
                max-width: 1000px;
                margin: 0 auto;
            }
            h1 {
                font-size: 2rem;
                margin-bottom: 0.5rem;
                color: #f8fafc;
            }
            .subtitle {
                color: #94a3b8;
                margin-bottom: 2rem;
            }
            .back-link {
                display: inline-block;
                margin-bottom: 1rem;
                color: #3b82f6;
                text-decoration: none;
            }
            .back-link:hover {
                text-decoration: underline;
            }
            .step {
                background: #1e293b;
                border-radius: 12px;
                padding: 1.5rem;
                margin-bottom: 1.5rem;
                border: 1px solid #334155;
            }
            .step.active {
                border-color: #3b82f6;
            }
            .step.complete {
                border-color: #22c55e;
                opacity: 0.7;
            }
            .step-header {
                display: flex;
                align-items: center;
                margin-bottom: 1rem;
            }
            .step-number {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                background: #334155;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 1rem;
                font-weight: bold;
            }
            .step.active .step-number {
                background: #3b82f6;
            }
            .step.complete .step-number {
                background: #22c55e;
            }
            .step-title {
                font-size: 1.25rem;
                color: #f1f5f9;
            }
            .form-group {
                margin-bottom: 1rem;
            }
            label {
                display: block;
                margin-bottom: 0.5rem;
                color: #94a3b8;
                font-size: 0.875rem;
            }
            input[type="text"],
            input[type="url"],
            textarea {
                width: 100%;
                padding: 0.75rem;
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 8px;
                color: #e2e8f0;
                font-family: inherit;
                font-size: 1rem;
            }
            textarea {
                min-height: 100px;
                resize: vertical;
            }
            input:focus,
            textarea:focus {
                outline: none;
                border-color: #3b82f6;
            }
            .btn {
                padding: 0.75rem 1.5rem;
                border: none;
                border-radius: 8px;
                font-size: 0.875rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                margin-right: 0.5rem;
            }
            .btn-primary {
                background: #3b82f6;
                color: white;
            }
            .btn-primary:hover:not(:disabled) {
                background: #2563eb;
            }
            .btn-secondary {
                background: #475569;
                color: white;
            }
            .btn-secondary:hover:not(:disabled) {
                background: #334155;
            }
            .btn-success {
                background: #22c55e;
                color: white;
            }
            .btn-success:hover:not(:disabled) {
                background: #16a34a;
            }
            .btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }
            .loading {
                display: inline-block;
                margin-left: 0.5rem;
            }
            .loading:after {
                content: '...';
                animation: dots 1.5s steps(4, end) infinite;
            }
            @keyframes dots {
                0%, 20% { content: '.'; }
                40% { content: '..'; }
                60%, 100% { content: '...'; }
            }
            .results {
                background: #0f172a;
                border-radius: 8px;
                padding: 1rem;
                margin-top: 1rem;
            }
            .post-sample {
                padding: 0.75rem;
                margin-bottom: 0.5rem;
                background: #1e293b;
                border-radius: 6px;
                border-left: 3px solid #3b82f6;
            }
            .post-sample h4 {
                color: #f1f5f9;
                margin-bottom: 0.25rem;
            }
            .post-sample .post-url {
                color: #94a3b8;
                font-size: 0.875rem;
                word-break: break-all;
            }
            .post-sample .post-date {
                color: #64748b;
                font-size: 0.75rem;
                margin-top: 0.25rem;
            }
            .error {
                background: #7f1d1d;
                border: 1px solid #991b1b;
                color: #fca5a5;
                padding: 1rem;
                border-radius: 8px;
                margin-top: 1rem;
            }
            .success {
                background: #14532d;
                border: 1px solid #166534;
                color: #86efac;
                padding: 1rem;
                border-radius: 8px;
                margin-top: 1rem;
            }
            .warning {
                background: #78350f;
                border: 1px solid #92400e;
                color: #fcd34d;
                padding: 1rem;
                border-radius: 8px;
                margin-top: 1rem;
            }
            .hidden {
                display: none;
            }
            pre {
                background: #0f172a;
                padding: 1rem;
                border-radius: 6px;
                overflow-x: auto;
                font-size: 0.875rem;
                margin-top: 0.5rem;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-link">← Back to Dashboard</a>
            <h1>Add New Blog</h1>
            <p class="subtitle">Generate a scraping schema and add a new blog to monitor</p>

            <!-- Step 1: Enter URL -->
            <div id="step1" class="step active">
                <div class="step-header">
                    <div class="step-number">1</div>
                    <div class="step-title">Enter Blog URL</div>
                </div>
                <div class="form-group">
                    <label for="blogUrl">Blog URL</label>
                    <input type="url" id="blogUrl" placeholder="https://example.com/blog" required>
                </div>
                <button class="btn btn-primary" onclick="generateSchema()" id="generateBtn">
                    Generate Schema
                </button>
            </div>

            <!-- Step 2: Review Schema & Samples -->
            <div id="step2" class="step hidden">
                <div class="step-header">
                    <div class="step-number">2</div>
                    <div class="step-title">Review Generated Schema</div>
                </div>
                <div id="schemaResults"></div>
                <div id="samplePosts"></div>
                <div style="margin-top: 1rem;">
                    <button class="btn btn-success" onclick="confirmAndSave()" id="saveBtn">
                        ✓ Looks Good - Add Blog
                    </button>
                    <button class="btn btn-secondary" onclick="showRefineStep()" id="refineBtn">
                        🔧 Refine Schema
                    </button>
                    <button class="btn btn-secondary" onclick="resetFlow()">
                        ↻ Start Over
                    </button>
                </div>
            </div>

            <!-- Step 3: Refine Schema (Optional) -->
            <div id="step3" class="step hidden">
                <div class="step-header">
                    <div class="step-number">3</div>
                    <div class="step-title">Refine Schema</div>
                </div>
                <div class="form-group">
                    <label for="feedback">What's wrong with the current schema?</label>
                    <textarea id="feedback" placeholder="Example: No posts found, or the dates are wrong, or titles are missing..."></textarea>
                </div>
                <button class="btn btn-primary" onclick="refineSchema()" id="refineSubmitBtn">
                    Refine Schema
                </button>
                <button class="btn btn-secondary" onclick="cancelRefine()">
                    Cancel
                </button>
                <div id="refineResults"></div>
            </div>

            <!-- Step 4: Success -->
            <div id="step4" class="step hidden">
                <div class="step-header">
                    <div class="step-number">✓</div>
                    <div class="step-title">Blog Added Successfully!</div>
                </div>
                <div id="successMessage"></div>
                <div style="margin-top: 1rem;">
                    <a href="/" class="btn btn-primary">Back to Dashboard</a>
                    <button class="btn btn-secondary" onclick="resetFlow()">Add Another Blog</button>
                </div>
            </div>
        </div>

        <script>
            let currentSchema = null;
            let currentUrl = null;

            async function generateSchema() {
                const url = document.getElementById('blogUrl').value.trim();
                if (!url) {
                    alert('Please enter a blog URL');
                    return;
                }

                currentUrl = url;
                const btn = document.getElementById('generateBtn');
                btn.disabled = true;
                btn.innerHTML = 'Generating<span class="loading"></span>';

                try {
                    const response = await fetch('/schema?sample=true', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ url })
                    });

                    const data = await response.json();

                    if (!data.success) {
                        showError('schemaResults', 'Failed to generate schema: ' + (data.error || 'Unknown error'));
                        btn.disabled = false;
                        btn.innerHTML = 'Generate Schema';
                        return;
                    }

                    currentSchema = data.schema;
                    displaySchema(data);

                    document.getElementById('step1').classList.add('complete');
                    document.getElementById('step1').classList.remove('active');
                    document.getElementById('step2').classList.remove('hidden');
                    document.getElementById('step2').classList.add('active');

                } catch (error) {
                    showError('schemaResults', 'Network error: ' + error.message);
                    btn.disabled = false;
                    btn.innerHTML = 'Generate Schema';
                }
            }

            function displaySchema(data) {
                const schemaDiv = document.getElementById('schemaResults');
                const postsDiv = document.getElementById('samplePosts');

                schemaDiv.innerHTML = '<h4 style="color: #f1f5f9; margin-bottom: 0.5rem;">Generated Schema:</h4>' +
                    '<pre>' + JSON.stringify(data.schema, null, 2) + '</pre>';

                if (data.sample_posts && data.sample_posts.length > 0) {
                    postsDiv.innerHTML = '<h4 style="color: #f1f5f9; margin: 1rem 0 0.5rem 0;">Sample Posts Found (' +
                        data.sample_posts.length + '):</h4>' +
                        data.sample_posts.map(post =>
                            '<div class="post-sample">' +
                            '<h4>' + (post.title || 'No title') + '</h4>' +
                            '<div class="post-url">' + (post.post_url || 'No URL') + '</div>' +
                            (post.date ? '<div class="post-date">Date: ' + post.date + '</div>' : '') +
                            '</div>'
                        ).join('');
                } else {
                    postsDiv.innerHTML = '<div class="warning">⚠️ No posts found with this schema. You may want to refine it.</div>';
                }

                if (data.error) {
                    postsDiv.innerHTML += '<div class="error">Validation Error: ' + data.error + '</div>';
                }
            }

            function showRefineStep() {
                document.getElementById('step2').classList.remove('active');
                document.getElementById('step3').classList.remove('hidden');
                document.getElementById('step3').classList.add('active');
            }

            function cancelRefine() {
                document.getElementById('step3').classList.add('hidden');
                document.getElementById('step3').classList.remove('active');
                document.getElementById('step2').classList.add('active');
            }

            async function refineSchema() {
                const feedback = document.getElementById('feedback').value.trim();
                if (!feedback) {
                    alert('Please provide feedback on what needs to be improved');
                    return;
                }

                const btn = document.getElementById('refineSubmitBtn');
                btn.disabled = true;
                btn.innerHTML = 'Refining<span class="loading"></span>';

                try {
                    const response = await fetch('/schema/refine?sample=true', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: currentUrl,
                            previous_schema: currentSchema,
                            feedback: feedback
                        })
                    });

                    const data = await response.json();

                    if (!data.success) {
                        showError('refineResults', 'Failed to refine schema: ' + (data.error || 'Unknown error'));
                        btn.disabled = false;
                        btn.innerHTML = 'Refine Schema';
                        return;
                    }

                    currentSchema = data.refined_schema;

                    // Show refined results
                    const resultsDiv = document.getElementById('refineResults');
                    resultsDiv.innerHTML = '<div class="success">✓ Schema refined successfully!</div>' +
                        '<h4 style="color: #f1f5f9; margin: 1rem 0 0.5rem 0;">Refined Schema:</h4>' +
                        '<pre>' + JSON.stringify(data.refined_schema, null, 2) + '</pre>';

                    if (data.sample_posts && data.sample_posts.length > 0) {
                        resultsDiv.innerHTML += '<h4 style="color: #f1f5f9; margin: 1rem 0 0.5rem 0;">Sample Posts (' +
                            data.sample_posts.length + '):</h4>' +
                            data.sample_posts.map(post =>
                                '<div class="post-sample">' +
                                '<h4>' + (post.title || 'No title') + '</h4>' +
                                '<div class="post-url">' + (post.post_url || 'No URL') + '</div>' +
                                (post.date ? '<div class="post-date">Date: ' + post.date + '</div>' : '') +
                                '</div>'
                            ).join('');

                        resultsDiv.innerHTML += '<div style="margin-top: 1rem;">' +
                            '<button class="btn btn-success" onclick="confirmAndSave()">✓ Looks Good - Add Blog</button>' +
                            '<button class="btn btn-secondary" onclick="refineAgain()">🔧 Refine Again</button>' +
                            '</div>';
                    } else {
                        resultsDiv.innerHTML += '<div class="warning">⚠️ Still no posts found. Try refining again with different feedback.</div>' +
                            '<button class="btn btn-secondary" onclick="refineAgain()" style="margin-top: 1rem;">🔧 Try Again</button>';
                    }

                    btn.disabled = false;
                    btn.innerHTML = 'Refine Schema';

                } catch (error) {
                    showError('refineResults', 'Network error: ' + error.message);
                    btn.disabled = false;
                    btn.innerHTML = 'Refine Schema';
                }
            }

            function refineAgain() {
                document.getElementById('feedback').value = '';
                document.getElementById('refineResults').innerHTML = '';
            }

            async function confirmAndSave() {
                if (!confirm('Add this blog to the database?')) {
                    return;
                }

                const saveBtn = document.getElementById('saveBtn');
                if (saveBtn) {
                    saveBtn.disabled = true;
                    saveBtn.innerHTML = 'Saving<span class="loading"></span>';
                }

                try {
                    const response = await fetch('/blogs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: currentUrl,
                            scraping_schema: currentSchema,
                            validate_schema: false  // Already validated
                        })
                    });

                    const data = await response.json();

                    if (!data.success) {
                        alert('Failed to add blog: ' + (data.error || data.message || 'Unknown error'));
                        if (saveBtn) {
                            saveBtn.disabled = false;
                            saveBtn.innerHTML = '✓ Looks Good - Add Blog';
                        }
                        return;
                    }

                    // Show success
                    document.getElementById('step2').classList.add('complete');
                    document.getElementById('step2').classList.remove('active');
                    document.getElementById('step3').classList.add('hidden');
                    document.getElementById('step4').classList.remove('hidden');
                    document.getElementById('step4').classList.add('active');

                    document.getElementById('successMessage').innerHTML =
                        '<div class="success">' +
                        '<h3 style="margin-bottom: 0.5rem;">✓ Blog Added Successfully!</h3>' +
                        '<p>Blog ID: ' + data.blog_id + '</p>' +
                        '<p>Name: ' + data.name + '</p>' +
                        '<p>Status: ' + data.status + '</p>' +
                        '<p style="margin-top: 1rem;">The blog will be checked automatically on the next scheduled run.</p>' +
                        '</div>';

                } catch (error) {
                    alert('Network error: ' + error.message);
                    if (saveBtn) {
                        saveBtn.disabled = false;
                        saveBtn.innerHTML = '✓ Looks Good - Add Blog';
                    }
                }
            }

            function showError(elementId, message) {
                const element = document.getElementById(elementId);
                element.innerHTML = '<div class="error">' + message + '</div>';
            }

            function resetFlow() {
                document.getElementById('blogUrl').value = '';
                document.getElementById('feedback').value = '';
                document.getElementById('schemaResults').innerHTML = '';
                document.getElementById('samplePosts').innerHTML = '';
                document.getElementById('refineResults').innerHTML = '';
                document.getElementById('successMessage').innerHTML = '';

                document.getElementById('step1').classList.remove('complete', 'hidden');
                document.getElementById('step1').classList.add('active');
                document.getElementById('step2').classList.add('hidden');
                document.getElementById('step2').classList.remove('active', 'complete');
                document.getElementById('step3').classList.add('hidden');
                document.getElementById('step3').classList.remove('active');
                document.getElementById('step4').classList.add('hidden');
                document.getElementById('step4').classList.remove('active');

                document.getElementById('generateBtn').disabled = false;
                document.getElementById('generateBtn').innerHTML = 'Generate Schema';

                currentSchema = null;
                currentUrl = null;
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker and monitoring."""
    try:
        # Test database connection
        conn = get_connection()
        conn.close()

        status = get_scheduler_status()

        return JSONResponse(
            content={
                "status": "healthy",
                "timestamp": datetime.utcnow().isoformat(),
                "scheduler_running": status["scheduler_running"],
                "database": "connected",
            }
        )
    except Exception as e:
        logger.error("Health check failed", extra={"error": str(e)})
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e),
            },
        )


@app.get("/status")
async def get_status():
    """Get current server status and scheduler information."""
    config = get_config()
    status = get_scheduler_status()

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as count FROM blogs WHERE scraping_successful = true")
        active_blogs = cursor.fetchone()["count"]  # type: ignore

        cursor.execute("SELECT COUNT(*) as count FROM blogs WHERE scraping_successful = false")
        error_blogs = cursor.fetchone()["count"]  # type: ignore

        cursor.execute("SELECT COUNT(*) as count FROM posts")
        total_posts = cursor.fetchone()["count"]  # type: ignore

        conn.close()

        return {
            "status": "running",
            "timestamp": datetime.utcnow().isoformat(),
            "config": {
                "check_interval_hours": config.check_interval_hours,
                "newsletter_window_hours": config.newsletter_window_hours,
                "log_level": config.log_level,
            },
            "scheduler": status,
            "database": {
                "active_blogs": active_blogs,
                "error_blogs": error_blogs,
                "total_posts": total_posts,
            },
        }
    except Exception as e:
        logger.error("Failed to get status", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/check")
async def trigger_check(background_tasks: BackgroundTasks, blog_id: int | None = None):
    """Manually trigger a blog check."""
    logger.info("Manual blog check triggered", extra={"blog_id": blog_id})

    def run_check():
        try:
            result = run_blog_check(blog_id=blog_id, max_workers=get_config().max_workers)
            logger.info("Manual blog check completed", extra={"success": result.success})
        except Exception as e:
            logger.error("Manual blog check failed", extra={"error": str(e)}, exc_info=True)

    background_tasks.add_task(run_check)

    return {
        "message": "Blog check started",
        "blog_id": blog_id,
        "mode": "single" if blog_id else "all",
    }


@app.post("/newsletter")
async def trigger_newsletter(hour_window: int = 24):
    """Manually trigger newsletter send."""
    logger.info("Manual newsletter send triggered", extra={"hour_window": hour_window})

    try:
        success, n_posts, error = send_newsletter_if_needed(hour_window=hour_window)

        if not success:
            raise HTTPException(status_code=500, detail=error or "Newsletter send failed")

        return {
            "message": "Newsletter sent successfully" if n_posts > 0 else "No posts to send",
            "posts_count": n_posts,
        }
    except Exception as e:
        logger.error("Manual newsletter send failed", extra={"error": str(e)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/blogs")
async def list_blogs():
    """List all blogs with their status."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                url,
                CASE
                    WHEN scraping_successful THEN 'Active'
                    ELSE 'Error'
                END as status,
                last_checked,
                created_at
            FROM blogs
            ORDER BY url
            """
        )
        blogs = cursor.fetchall()
        conn.close()

        return {"blogs": [dict(blog) for blog in blogs]}  # type: ignore
    except Exception as e:
        logger.error("Failed to list blogs", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/posts/recent")
async def get_recent_posts(limit: int = 20):
    """Get recently discovered posts."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                p.id,
                p.title,
                p.url,
                p.publication_date,
                p.discovered_date,
                p.reading_time,
                p.summary,
                b.url as blog_name,
                STRING_AGG(t.name, ', ' ORDER BY t.name) as topics
            FROM posts p
            LEFT JOIN blogs b ON b.id = p.blog_id
            LEFT JOIN post_topics pt ON p.id = pt.post_id
            LEFT JOIN topics t ON t.id = pt.topic_id
            GROUP BY p.id, b.url
            ORDER BY p.discovered_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        posts = cursor.fetchall()
        conn.close()

        return {"posts": [dict(post) for post in posts], "count": len(posts)}  # type: ignore
    except Exception as e:
        logger.error("Failed to get recent posts", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/logs")
async def get_logs(lines: int = 100):
    """Get recent log entries."""
    import json

    try:
        log_dir = Path("/app/logs" if os.path.exists("/app/logs") else "logs")
        log_file = log_dir / "blogregator.log"

        if not log_file.exists():
            return {"logs": [], "message": "Log file not found"}

        # Read last N lines from log file
        with open(log_file) as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        # Parse JSON log entries
        log_entries = []
        for line in recent_lines:
            try:
                log_entry = json.loads(line.strip())
                log_entries.append(log_entry)
            except json.JSONDecodeError:
                # If not JSON, just include as plain text
                log_entries.append({"message": line.strip(), "levelname": "INFO"})

        return {"logs": log_entries, "count": len(log_entries)}
    except Exception as e:
        logger.error("Failed to read logs", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/schema")
async def generate_blog_schema(request: SchemaGenerationRequest, sample: bool = Query(False)):
    """Generate a scraping schema for a blog URL using LLM.

    Args:
        request: Request containing blog URL
        sample: If True, validate schema and return sample parsed posts

    Returns:
        Generated schema with optional validation results
    """
    logger.info("Schema generation requested", extra={"url": request.url, "sample": sample})

    try:
        # Fetch HTML content
        logger.debug("Fetching blog HTML", extra={"url": request.url})
        try:
            response = fetch_with_retries(request.url)
            html_content = response.content
        except Exception as e:
            logger.error("Failed to fetch blog URL", extra={"url": request.url, "error": str(e)})
            raise HTTPException(
                status_code=400, detail=f"Failed to fetch blog URL: {str(e)}"
            ) from e

        # Extract body content
        soup = BeautifulSoup(html_content, "html.parser")
        body_content = str(soup.body) if soup.body else str(soup)

        # Generate schema using LLM
        logger.debug("Generating schema with LLM", extra={"url": request.url})
        try:
            schema = generate_schema(body_content, request.url)
        except Exception as e:
            logger.error("Failed to generate schema", extra={"url": request.url, "error": str(e)})
            raise HTTPException(
                status_code=500, detail=f"Failed to generate schema: {str(e)}"
            ) from e

        response_data = {
            "url": request.url,
            "schema": schema,
            "success": True,
            "sample_posts": None,
            "error": None,
        }

        # Optionally validate schema by parsing posts
        if sample:
            logger.debug("Validating schema with sample parse", extra={"url": request.url})
            try:
                sample_posts = parse_post_list(request.url, schema)
                response_data["sample_posts"] = sample_posts[:5]  # Return max 5 samples
                logger.info(
                    "Schema validated successfully",
                    extra={"url": request.url, "posts_found": len(sample_posts)},
                )
            except Exception as e:
                logger.warning(
                    "Schema validation failed", extra={"url": request.url, "error": str(e)}
                )
                response_data["error"] = f"Schema validation failed: {str(e)}"

        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Unexpected error in schema generation", extra={"error": str(e)}, exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}") from e


@app.post("/schema/refine")
async def refine_blog_schema(request: RefineSchemaRequest, sample: bool = Query(False)):
    """Refine an existing scraping schema based on user feedback using LLM.

    Args:
        request: Request containing blog URL, previous schema, and user feedback
        sample: If True, validate refined schema and return sample parsed posts

    Returns:
        Refined schema with optional validation results
    """
    logger.info(
        "Schema refinement requested",
        extra={"url": request.url, "feedback_length": len(request.feedback), "sample": sample},
    )

    try:
        # Fetch HTML content
        logger.debug("Fetching blog HTML for refinement", extra={"url": request.url})
        try:
            response = fetch_with_retries(request.url)
            html_content = response.content
        except Exception as e:
            logger.error(
                "Failed to fetch blog URL for refinement",
                extra={"url": request.url, "error": str(e)},
            )
            raise HTTPException(
                status_code=400, detail=f"Failed to fetch blog URL: {str(e)}"
            ) from e

        # Extract body content
        soup = BeautifulSoup(html_content, "html.parser")
        body_content = str(soup.body) if soup.body else str(soup)

        # Try to parse with previous schema to get results
        previous_results = ""
        parse_error = ""
        try:
            posts = parse_post_list(request.url, request.previous_schema)
            if posts:
                # Format first 3 posts for display
                previous_results = "\n\n".join(
                    [
                        f"Post {i}:\nTitle: {p.get('title', 'No title')}\nURL: {p.get('post_url', 'No URL')}\nDate: {p.get('date', 'No date')}"
                        for i, p in enumerate(posts[:3], 1)
                    ]
                )
            else:
                previous_results = "No posts were found using the previous schema."
        except Exception as e:
            parse_error = str(e)
            previous_results = "Failed to parse posts with previous schema."

        # Format the CORRECT_SCHEMA prompt
        formatted_prompt = CORRECT_SCHEMA.format(
            previous_schema=json.dumps(request.previous_schema, indent=2),
            previous_results=previous_results,
            error=parse_error,
            user_feedback=request.feedback,
            blog_url=request.url,
            html_content=body_content,
        )

        # Generate refined schema using LLM
        logger.debug("Refining schema with LLM", extra={"url": request.url})
        try:
            refined_schema = generate_json_from_llm(formatted_prompt)
        except Exception as e:
            logger.error("Failed to refine schema", extra={"url": request.url, "error": str(e)})
            raise HTTPException(status_code=500, detail=f"Failed to refine schema: {str(e)}") from e

        response_data = {
            "url": request.url,
            "previous_schema": request.previous_schema,
            "refined_schema": refined_schema,
            "success": True,
            "sample_posts": None,
            "error": None,
        }

        # Optionally validate refined schema by parsing posts
        if sample:
            logger.debug("Validating refined schema with sample parse", extra={"url": request.url})
            try:
                sample_posts = parse_post_list(request.url, refined_schema)
                response_data["sample_posts"] = sample_posts[:5]  # Return max 5 samples
                logger.info(
                    "Refined schema validated successfully",
                    extra={"url": request.url, "posts_found": len(sample_posts)},
                )
            except Exception as e:
                logger.warning(
                    "Refined schema validation failed",
                    extra={"url": request.url, "error": str(e)},
                )
                response_data["error"] = f"Refined schema validation failed: {str(e)}"

        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Unexpected error in schema refinement", extra={"error": str(e)}, exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}") from e


@app.post("/blogs")
async def add_blog(request: AddBlogRequest, overwrite: bool = Query(False)):
    """Add a new blog to the database with a provided schema.

    Args:
        request: Request containing blog URL, optional name, schema, and validation flag
        overwrite: If True, overwrite existing blog with same URL

    Returns:
        Blog creation result with ID and status
    """
    logger.info(
        "Add blog requested",
        extra={
            "url": request.url,
            "blog_name": request.name,
            "validate": request.validate_schema,
            "overwrite": overwrite,
        },
    )

    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Check if blog already exists
        cursor.execute("SELECT id FROM blogs WHERE url = %s", (request.url,))
        existing_blog = cursor.fetchone()

        if existing_blog and not overwrite:
            conn.close()
            logger.warning(
                "Blog already exists", extra={"url": request.url, "blog_id": existing_blog["id"]}
            )  # type: ignore
            raise HTTPException(
                status_code=409,
                detail=f"Blog with URL {request.url} already exists (ID: {existing_blog['id']}). Use ?overwrite=true to update.",  # type: ignore
            )

        # Note: The database schema doesn't have a 'name' column
        # We'll use the URL for display purposes and ignore request.name
        display_name = request.name if request.name else get_domain_name(request.url)

        validation_results = None
        scraping_successful = True

        # Validate schema if requested
        if request.validate_schema:
            logger.debug("Validating schema", extra={"url": request.url})
            try:
                posts = parse_post_list(request.url, request.scraping_schema)
                validation_results = {
                    "posts_found": len(posts),
                    "sample_posts": posts[:3] if posts else [],  # Return max 3 samples
                }

                if not posts:
                    scraping_successful = False
                    logger.warning(
                        "Schema validation returned no posts", extra={"url": request.url}
                    )
                else:
                    logger.info(
                        "Schema validated successfully",
                        extra={"url": request.url, "posts_found": len(posts)},
                    )

            except Exception as e:
                scraping_successful = False
                validation_results = {"posts_found": 0, "error": str(e)}
                logger.warning(
                    "Schema validation failed", extra={"url": request.url, "error": str(e)}
                )

        # Save to database
        schema_json = json.dumps(request.scraping_schema)

        if existing_blog:
            # Update existing blog
            cursor.execute(
                """
                UPDATE blogs
                SET scraping_schema = %s, scraping_successful = %s, last_modified_at = NOW()
                WHERE url = %s
                RETURNING id
                """,
                (schema_json, scraping_successful, request.url),
            )
            result = cursor.fetchone()
            blog_id = result["id"]  # type: ignore
            message = f"Blog '{display_name}' updated successfully"
            logger.info("Blog updated", extra={"blog_id": blog_id, "url": request.url})
        else:
            # Insert new blog
            cursor.execute(
                """
                INSERT INTO blogs (url, scraping_schema, scraping_successful)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (request.url, schema_json, scraping_successful),
            )
            result = cursor.fetchone()
            blog_id = result["id"]  # type: ignore
            message = f"Blog '{display_name}' added successfully"
            logger.info("Blog added", extra={"blog_id": blog_id, "url": request.url})

        conn.commit()
        conn.close()

        return {
            "success": True,
            "blog_id": blog_id,
            "name": display_name,
            "url": request.url,
            "status": "Active" if scraping_successful else "Error",
            "validation_results": validation_results,
            "message": message,
            "error": None,
        }

    except HTTPException:
        if conn:
            conn.close()
        raise
    except Exception as e:
        logger.error(
            "Failed to add blog", extra={"url": request.url, "error": str(e)}, exc_info=True
        )
        if conn:
            conn.rollback()
            conn.close()
        raise HTTPException(status_code=500, detail=f"Failed to add blog: {str(e)}") from e


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    stop_scheduler()
    sys.exit(0)


def main():
    """Main entry point for the server."""
    # Load config and setup logging first
    try:
        get_config()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    setup_logging()
    logger.info("Starting Blogregator server...")

    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Run server
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,  # We handle logging ourselves
        access_log=False,  # Disable noisy access logs
    )


if __name__ == "__main__":
    main()
