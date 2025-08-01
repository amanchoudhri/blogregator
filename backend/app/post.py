from typing import Any, Mapping, Optional
from dataclasses import dataclass

import typer

from bs4 import BeautifulSoup

import psycopg

from .database import get_connection
from .llm import generate_json_from_llm
from .utils import fetch_with_retries, multiline_user_input

@dataclass
class PostProcessingResult:
    original_post: dict
    success: bool
    
    extracted_text: Optional[str] = None
    
    summary: Optional[str] = None
    reading_time: Optional[int] = None
    topics: Optional[list] = None
    
    error_type: Optional[str] = None
    error_message: Optional[str] = None

post_cli = typer.Typer(
    name="post",
    help="Manage and view blog posts."
)

@post_cli.command(name="view")
def view_post(
    post_id: int = typer.Argument(..., help="ID of the post to view")
):
    """View detailed information for a single post."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            p.id,
            p.title, 
            p.url, 
            p.publication_date, 
            p.reading_time, 
            p.summary,
            STRING_AGG(t.name, ', ' ORDER BY t.name) as topics
        FROM posts p
        LEFT JOIN post_topics tp ON p.id = tp.post_id
        LEFT JOIN topics t ON t.id = tp.topic_id
        WHERE p.id = %s
        GROUP BY p.id;
        """, (post_id,)
    )

    row: Mapping[str, Any] = cursor.fetchone() # type: ignore
    conn.close()

    if not row:
        typer.echo("Post not found.")
        return

    typer.echo(typer.style(row['title'], bold=True))
    typer.echo(f"URL: {row['url']}")
    typer.echo(f"Published: {row['publication_date']}")
    if row.get('topics'):
        typer.echo(f"\nTopics: {row['topics']}")
    if row.get('reading_time'):
        typer.echo(f"Reading time: {row['reading_time']} min")
    if row.get('summary'):
        typer.echo("\nSummary:\n" + row['summary'])

@post_cli.command(name="list")
def list_posts(
    blog_name: str = typer.Argument(..., help="Name of the blog"),
    limit: int = typer.Option(10, "-n", help="Max number of posts to display")
):
    """View recent posts for a specific blog."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM blogs WHERE name = %s", (blog_name,))
    result: dict[str, int] | None = cursor.fetchone() # type: ignore
    if result is None:
        typer.echo("Blog not found.")
        return

    blog_id = result['id']
    cursor.execute(
        "SELECT id, title, publication_date, url FROM posts WHERE blog_id = %s "
        "ORDER BY publication_date DESC LIMIT %s", (blog_id, limit)
    )
    posts: list[Mapping[str, Any]] = cursor.fetchall() # type: ignore
    conn.close()

    if not posts:
        typer.echo("No posts found for this blog.")
        return

    typer.echo(typer.style(f"{'ID':<4} {'Published':<12} {'Title'}", bold=True))
    for p in posts:
        if p.get('publication_date'):
            pub = p['publication_date'].strftime('%Y-%m-%d')
        else:
            pub = ""
        typer.echo(f"{p['id']:<4} {pub:<12} {p['title']}")
        
@post_cli.command(name="reparse")
def reparse_post(
    url: str = typer.Argument(help="URL of the post to reparse"),
    manually_paste_content: bool = typer.Option(False, "--paste-content", help="Flag to manually paste the post content after the command.")
    ):
    """Reparse a specific post."""
    # get the post ID
    conn = get_connection()
    curr = conn.cursor()
    
    curr.execute("SELECT blog_id, title, url AS post_url, publication_date AS date FROM posts WHERE url = %s", (url,))
    result: dict[str, Any] | None = curr.fetchone() # type: ignore
    
    if result is None:
        typer.echo("Post not found.")
        return
    
    if manually_paste_content:
        post_content = multiline_user_input()
    else:
        post_content = None
        
    # Legacy interface - process and save to DB
    result_obj = process_single_post(result, post_content)
    if any([result_obj.summary, result_obj.reading_time, result_obj.topics]):
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Add new topics to database if any
            if result_obj.topics:
                cursor.executemany(
                    "INSERT INTO topics (name) VALUES %s ON CONFLICT DO NOTHING",
                    [(t,) for t in result_obj.topics]
                )
            
            # Add the post
            metadata = {
                'summary': result_obj.summary,
                'reading_time': result_obj.reading_time,
                'matched_topics': result_obj.topics or [],
                'new_topic_suggestions': []
            }
            add_post_to_db(cursor, result['blog_id'], result, metadata, upsert=True)
            conn.commit()
            typer.echo("Post reprocessed successfully.")
        except Exception as e:
            typer.echo(typer.style(f"Error saving post: {e}", fg=typer.colors.RED))
        finally:
            conn.close()
    else:
        typer.echo(typer.style("Post processing failed - no content extracted", fg=typer.colors.RED))
    

def process_single_post(post: dict[str, Any], post_text: str | None = None) -> PostProcessingResult:
    """
    Extract metadata from a post without writing to database.
    
    Args:
        post: A dictionary containing the post information. Expects keys:
            - title: The title of the post
            - post_url: The URL of the post
            - date: The publication date of the post
        post_text: Optional pre-fetched post content
    
    Returns:
        PostProcessingResult: Processing results with extracted metadata and error info
    """
    
    result = PostProcessingResult(
        original_post=post,
        success=False,
    )
    
    try:
        typer.echo(f"Processing post: {post['post_url']}")
        
        # Try to get post content
        try:
            if post_text is None:
                content = fetch_with_retries(post['post_url']).text
                result.extracted_text = extract_post_text(content)
            else:
                result.extracted_text = post_text
        except Exception as e:
            result.error_type = "network"
            result.error_message = str(e)
            return result
        
        # Attempt all three LLM extractions
        llm_errors = []
        
        # Extract summary
        try:
            summary_data = extract_summary(result.extracted_text)
            result.summary = summary_data.get('summary')
            technical_density = summary_data.get('technical_density', 2)
        except Exception as e:
            llm_errors.append(f"summary: {str(e)}")
            technical_density = 2
        
        # Extract reading time
        try:
            result.reading_time = estimate_reading_time(result.extracted_text, technical_density)
        except Exception as e:
            llm_errors.append(f"reading_time: {str(e)}")
        
        # Extract topics
        try:
            # Get existing topics for context
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM topics")
            existing_topics = [row.get('name', '') for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            
            topics_data = extract_topics(result.extracted_text, existing_topics)
            result.topics = topics_data.get('matched_topics', []) + topics_data.get('new_topic_suggestions', [])
        except Exception as e:
            llm_errors.append(f"topics: {str(e)}")
        
        # Set success and error info
        if llm_errors:
            result.error_type = "llm"
            result.error_message = "; ".join(llm_errors)
        
        # Success only if ALL three fields are populated
        result.success = bool(result.summary and result.reading_time and result.topics)
        
    except Exception as e:
        # Unexpected error - treat as LLM error
        result.error_type = "llm"
        result.error_message = str(e)
        
    return result
        
def add_post_to_db(cursor, blog_id: int, post_info: dict[str, str], metadata: dict[str, Any], upsert: bool = False):
    """Add a post to the database if it isn't already registered."""
    upsert_clause = """
        ON CONFLICT (url)DO UPDATE SET
        summary = EXCLUDED.summary,
        reading_time = EXCLUDED.reading_time
    """

    cursor.execute(
        f"""INSERT INTO posts (blog_id, title, url, publication_date, reading_time, summary)
        VALUES (%s, %s, %s, %s, %s, %s) {upsert_clause if upsert else ""}
        """,
        (
            blog_id, post_info['title'], post_info['post_url'], post_info['date'],
            metadata['reading_time'], metadata['summary']
        )
    )
    cursor.execute("SELECT id FROM posts WHERE url = %s", (post_info['post_url'],))
    post_id = cursor.fetchone()['id']
    topics = metadata.get('matched_topics', []) + metadata.get('new_topic_suggestions', [])

    # get the IDs of each topic
    cursor.execute("SELECT id FROM topics WHERE name = ANY(%s)", (topics,))
    topic_ids = [row['id'] for row in cursor.fetchall()]

    cursor.executemany(
        """INSERT INTO post_topics (post_id, topic_id) VALUES %s ON CONFLICT DO NOTHING""",
        [(post_id, topic_id) for topic_id in topic_ids]
    )

def extract_post_metadata(
    post_url: str,
    post_text: str | None = None,
    model: str = "gemini/gemini-2.5-flash-preview-05-20"
    ) -> dict:
    """Extract post metadata."""
    metadata = {}

    text_content = post_text
    if text_content is None:
        content = fetch_with_retries(post_url).text
        text_content = extract_post_text(content)
        
    summary = extract_summary(text_content, model)
    metadata.update(summary)
    
    reading_time = estimate_reading_time(text_content, summary.get('technical_density', 2))
    metadata['reading_time'] = reading_time

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM topics")
    existing_topics: list[str] = [row.get('name', '') for row in cursor.fetchall()] # type: ignore
    cursor.close()
    conn.close()

    topics = extract_topics(text_content, existing_topics, model)
    metadata.update(topics)

    return metadata

def extract_post_text(html_content: str) -> str:
    """
    Extracts the main article text from HTML content.

    It looks for the <article> tag first, then falls back to the
    <body> tag if <article> is not found. It removes common non-content
    elements like nav, aside, header, and footer.

    Args:
        html_content: A string containing the HTML of the blog post.

    Returns:
        A string containing the cleaned text content of the post.
    """
    soup = BeautifulSoup(html_content, 'html.parser')

    # Find the main article content. The <article> tag is a strong semantic indicator.
    # If it doesn't exist (or if there are multiple), fall back to the main role, and finally the whole body.
    article_tag_instances = soup.find_all('article')
    article_body = article_tag_instances[0] if article_tag_instances else None
    if (article_body is None) or (len(article_tag_instances) > 1):
        article_body = soup.find(attrs={'role': 'main'})

    if article_body is None:
        if soup.body is not None:
            article_body = soup.body
        else:
            return "Unable to parse post content."
        
    # Remove common non-content elements to clean up the text
    for tag_to_remove in article_body(['nav', 'aside', 'header', 'footer', 'script', 'style']):
        tag_to_remove.decompose()

    # Get the text, with separators to preserve paragraph breaks.
    # The 'strip=True' argument removes leading/trailing whitespace from each line.
    text_content = article_body.get_text(separator='\n', strip=True)
    
    return text_content

def estimate_reading_time(content: str, technical_density: int) -> int:
    """Estimate reading time in minutes based on word count and technical complexity."""
    word_count = len(content.split())
    
    # Adjust WPM based on technical density
    wpm_map = {
        1: 220,  # Reflective/anecdotal - flows quickly
        2: 180,  # Practitioner content - need to think through examples  
        3: 100   # Deep technical - lots of pausing to understand
    }
    
    wpm = wpm_map.get(technical_density, 180)  # Default to level 2
    return max(1, round(word_count / wpm))

def extract_summary(content: str, model: str = "gemini/gemini-2.5-flash-preview-05-20") -> dict:
    """Extract summary and technical density."""
    prompt = SUMMARY_PROMPT.format(content=content)

    return generate_json_from_llm(
        prompt=prompt,
        model=model,
        response_schema=SUMMARY_SCHEMA,
        reasoning_effort="low"
    )

def extract_topics(
        content: str,
        existing_topics: list[str],
        model: str = "gemini/gemini-2.5-flash-preview-05-20"
    ) -> dict:
    """Extract topics from the blog post."""
    topic_string = ', '.join(existing_topics)
    prompt = TOPIC_PROMPT.format(content=content, existing_topics=topic_string)
    return generate_json_from_llm(
        prompt=prompt,
        model=model,
        response_schema=TOPIC_SCHEMA,
        reasoning_effort='low'
    )

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 sentence summary. Concise but fluid."
        },
        "technical_density": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": "Technical complexity: 1=reflective/anecdotal, 2=practitioner-oriented, 3=deeply technical/mathematical"
        }
    },
    "required": ["summary", "technical_density"]
}

SUMMARY_PROMPT = """
You are an expert content analyst. Analyze this blog post and provide:

1. A concise 2-3 sentence summary that captures the main point and key takeaway
2. Technical density rating (1-3 scale)

Target an intelligent and well-read reader. Write concisely, fluidly, and clearly. Prefer
specific details from the article over broad generalizations or abstract musings.

Here's a great summary example, for Ben Kuhn's "You don't need to solve hard problems":
    'The author challenges the common perception among academically-successful students that the most valuable work involves "solving hard technical problems." Through personal anecdotes from internships to startup roles at Wave, they illustrate that real-world impact often stems from identifying and efficiently solving *important* problems—even if technically simple—by optimizing for factors like speed, prioritization, or leveraging teams, which ultimately provides greater leverage than pursuing technical difficulty alone.'

Technical Density Scale with Examples:

**Level 1 - Reflective/Anecdotal:**
- Personal experiences, career reflections, life lessons
- Philosophy and high-level thinking about technology/work
- Examples: Paul Graham essays, "You don't need to solve hard problems" by Ben Kuhn
- Accessible to anyone, no specialized knowledge required

**Level 2 - Practitioner-Oriented:**
- Technical content for working professionals
- How-to guides, lessons learned, best practices
- Examples: Hex Labs blog on LLM evals, "Lessons learned in AI evals"
- Assumes domain familiarity but explains technical concepts

**Level 3 - Deeply Technical/Mathematical:**
- Research papers, mathematical derivations, system internals
- Advanced algorithms, formal methods, low-level technical details
- Examples: Linux kernel vulnerabilities, conjugate gradient descent details, Rao-Blackwellization
- Requires deep expertise to fully understand

Blog Post:
{content}

Return ONLY the JSON object, no additional text.
"""

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact kebab-case names from existing topics list that substantially match the content"
        },
        "new_topic_suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 new topics in kebab-case that represent genuinely new concepts with reusable granularity",
        }
    },
    "required": ["matched_topics"]
}

TOPIC_PROMPT = """
You are an expert content categorizer. Based on this blog post, identify relevant topics.

Existing Topics, from other articles:
{existing_topics}

**Matching Existing Topics (`matched_topics`):**
- Assign topics based on content from existing list, using EXACT names in kebab-case
- Match based on main concepts, not just keyword presence

**Suggesting New Topics (`new_topic_suggestions`):**
- Optionally, suggest 1-3 carefully-chosen new topics, if they represent genuinely NEW concepts not covered by existing list
- Output topics in kebab-case (lowercase with hyphens)
- Skip obvious variations of existing topics
- Avoid broad topics: "machine-learning", "ai", "startups", "programming", "data-science"
- Also avoid overly-specific ones: "grey-box-bayesian-optimization", "speculative-decoding"
- Ask yourself: "Would this topic apply to multiple future blog posts I might encounter?"
- BE CONSERVATIVE. Default to no new suggestions.

Blog Content:
{content}

Return ONLY the JSON object, no additional text.
"""
