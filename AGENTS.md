# Blogregator - AI Agent Development Guide

## Overview

Blogregator is a Python CLI tool for automatically scraping blogs, extracting metadata from posts, and sending email newsletters when new posts are discovered. It uses LLMs (via Google Gemini) to generate scraping schemas and extract post metadata.

**Tech Stack:**
- Python 3.11
- `uv` for package management
- PostgreSQL database (psycopg2)
- `typer` for CLI
- `litellm` for LLM integration (primarily Google Gemini)
- `BeautifulSoup` for HTML parsing
- `requests` for HTTP
- `ruff` for linting and formatting

## Development Setup

### Prerequisites
- Python 3.11
- `uv` package manager ([installation guide](https://github.com/astral-sh/uv))
- PostgreSQL database (local or hosted on Supabase/Neon)

### Initial Setup

```bash
# Clone and navigate to repository
cd /path/to/blogregator

# Install dependencies (including dev dependencies)
uv sync --extra dev

# Activate virtual environment
source .venv/bin/activate
```

### Environment Variables

Create a `.env` file or export these variables:

```bash
DATABASE_URL="postgresql://user:password@host:port/dbname"
GEMINI_API_KEY="your_gemini_api_key"
SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="your_email@example.com"
SMTP_PASSWORD="your_email_password"
EMAIL_TO="recipient@example.com"
```

### Database Initialization

```bash
blogregator init-db
```

## Running & Testing

### CLI Commands

Run commands using either method:
```bash
# Via module (always works)
uv run python -m blogregator.cli <command>

# Or via installed script (after uv sync)
blogregator <command>
```

### Common Commands

```bash
# Manage blogs
blogregator blog add https://example.com
blogregator blog list

# Check for new posts
blogregator run-check                    # Check all active blogs
blogregator run-check --blog-id 1 -y     # Check specific blog

# Manage posts
blogregator post list <blog-name>
blogregator post view <post-id>
blogregator post reparse <post-url>

# Send newsletter
blogregator send-newsletter              # Default: last 8 hours
blogregator send-newsletter --hour-window 24
```

### Manual Testing Workflow

Since there are no automated tests, verify changes by:

1. **Test with a real blog**: Use `blog add` with a test URL
2. **Run checks**: Use `run-check --blog-id <id> -y` to test scraping
3. **Inspect database**: Query PostgreSQL directly to verify data
4. **Test error handling**: Try invalid URLs, missing env vars, etc.

## Code Style & Standards

### Formatting & Linting

Always run `ruff` before committing changes:

```bash
# Check for issues
uv run ruff check .

# Auto-fix issues
uv run ruff check --fix .

# Format code
uv run ruff format .

# Fix import sorting specifically
uv run ruff check --select I --fix .
```

### Import Organization

Organize imports in three groups separated by blank lines:

```python
# Standard library
import json
import os
from typing import Annotated, Optional

# Third-party packages
import typer
from bs4 import BeautifulSoup

# Local imports
from blogregator.database import get_connection
from blogregator.utils import fetch_with_retries
```

### Type Hints

- Use type hints for all function parameters and return values
- Use Python 3.10+ union syntax: `str | None` instead of `Optional[str]`
- Use `dict[str, Any]` instead of `Dict[str, Any]` (Python 3.9+ style)

```python
def process_blog(conn, blog: dict[str, Any]) -> dict[str, int]:
    """Process a single blog and return metrics."""
    pass

def get_post(post_id: int | None) -> dict | None:
    """Retrieve post by ID if provided."""
    pass
```

### Naming Conventions

- **Functions/variables**: `snake_case` (e.g., `fetch_with_retries`, `blog_id`)
- **Classes**: `PascalCase` (e.g., `PostProcessingResult`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `SUMMARY_PROMPT`, `TOPIC_SCHEMA`)
- **Private helpers**: Prefix with `_` if truly internal
- **Descriptive names**: Prefer `post_url` over `url`, `blog_id` over `id`

### Error Handling

**Use specific exception types:**
```python
try:
    response = requests.get(url)
    response.raise_for_status()
except requests.RequestException as e:
    # Handle network errors
    log_error(cursor, blog_id, 'network', str(e))
```

**Implement retries for unreliable operations:**
```python
def fetch_with_retries(url: str, retries: int = 3, sleep: int = 1):
    for attempt in range(retries):
        try:
            return requests.get(url)
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(sleep)
            else:
                raise
```

**Return result objects for complex operations:**
```python
@dataclass
class PostProcessingResult:
    success: bool
    error_type: Optional[str] = None
    error_message: Optional[str] = None
```

### Database Patterns

**Always use parameterized queries:**
```python
# Good
cursor.execute("SELECT * FROM blogs WHERE id = %s", (blog_id,))

# Bad - SQL injection risk!
cursor.execute(f"SELECT * FROM blogs WHERE id = {blog_id}")
```

**Use RealDictCursor for dict-like access:**
```python
conn = psycopg2.connect(
    database_url,
    cursor_factory=psycopg2.extras.RealDictCursor
)
cursor = conn.cursor()
row = cursor.fetchone()
print(row['id'], row['name'])  # Access by column name
```

**Commit transactions explicitly:**
```python
try:
    cursor.execute("INSERT INTO ...")
    cursor.execute("UPDATE ...")
    conn.commit()
except Exception as e:
    conn.rollback()
    raise
finally:
    conn.close()
```

### LLM Integration Patterns

**Use `generate_json_from_llm` helper:**
```python
from blogregator.llm import generate_json_from_llm

result = generate_json_from_llm(
    prompt=formatted_prompt,
    model="gemini/gemini-2.5-flash-preview-05-20",
    response_schema=MY_SCHEMA,  # Optional
    reasoning_effort="low"       # Optional: "low", "medium", "high"
)
```

**Define schemas for structured outputs:**
```python
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "technical_density": {"type": "integer", "minimum": 1, "maximum": 3}
    },
    "required": ["summary", "technical_density"]
}
```

**Handle LLM failures gracefully:**
```python
try:
    result = generate_json_from_llm(prompt)
except Exception as e:
    # Log error but continue processing
    log_error(cursor, blog_id, 'llm', str(e))
```

## Architecture Overview

**Key modules:**
- `cli.py` - Main CLI entry point, orchestrates blog checking
- `blog.py` - Blog management (add, list, schema generation)
- `post.py` - Post processing (metadata extraction, text parsing)
- `parser.py` - HTML parsing using generated schemas
- `database.py` - Database connection and initialization
- `llm.py` - LLM interaction utilities
- `emails.py` - Email newsletter generation
- `utils.py` - Shared utilities (HTTP retries, date handling)
- `prompts.py` - LLM prompts for schema generation and correction

## Common Patterns

**Add a new CLI command:**
```python
@app.command(name="my-command")
def my_command(
    arg: Annotated[str, typer.Argument(help="Description")],
    option: Annotated[int, typer.Option(help="Description")] = 10
):
    """Command description."""
    typer.echo(f"Processing {arg} with {option}")
```

**Query the database:**
```python
conn = get_connection()
cursor = conn.cursor()
cursor.execute("SELECT * FROM posts WHERE blog_id = %s", (blog_id,))
posts = cursor.fetchall()
conn.close()
```

**Process posts in parallel:**
```python
import multiprocessing as mp

max_workers = min(os.cpu_count() or 4, len(items), 8)
with mp.Pool(processes=max_workers) as pool:
    results = list(pool.map(process_function, items))
```

## Deployment

The project uses GitHub Actions for scheduled checks:
- Workflow: `.github/workflows/blog-monitor.yml`
- Runs daily at midnight UTC
- Can be manually triggered via workflow_dispatch
- Requires secrets: `DATABASE_URL`, `GEMINI_API_KEY`, SMTP credentials

## Guidelines for AI Agents

1. **Always run ruff**: Before completing any changes, run `uv run ruff check --fix .` and `uv run ruff format .`
2. **Test manually**: Since there are no automated tests, manually test CLI commands with a real database
3. **Preserve type hints**: Maintain type hints for all new functions and modifications
4. **Follow error patterns**: Use try-except with specific exception types, implement retries for network/LLM calls
5. **Use parameterized queries**: Never use string interpolation for SQL queries
6. **Be conservative with prompts**: LLM prompts are carefully tuned; changes should be minimal and well-tested
7. **Document complex logic**: Add docstrings for new functions and inline comments for non-obvious code
8. **Handle partial failures**: Many operations (post processing, LLM extraction) can partially fail; design accordingly
