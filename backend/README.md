# blogregator

There are so many great blogs out there,
but it's hard to stay up to date—especially because many don't have RSS feeds. So I built Blogregator to help me keep track of
the latest posts from blogs I follow.

Blogregator is a tool for automatically scraping blogs, extracting metadata from posts, and updating you when a new post drops.

<div align="center">
<img src="images/email-example.png" alt="A Blogregator email newsletter showing new posts" width="400">
</div>

Currently, I'm using it to monitor 10+ blogs, and it has **already successfully processed over 400 posts** with 100% schema generation success rate.

## Demo
Adding blogs, checking for new posts, and sending newsletters is straightforward.
```bash
# LLM automatically generates scraping schema for any blog format
$ blogregator blog add https://example-blog.com
Fetching HTML content...
Generating parser function...
Found posts using the generated schema:

Post 1:
Title: How to Build Better ML Pipelines
URL: https://example-blog.com/ml-pipelines
Date: 2025-05-28

Does this look correct? [y/N]: y
Successfully added blog: example-blog (https://example-blog.com)

# Automated checks find new posts
$ blogregator run-check
Checking blog 'example-blog' (ID 1)...
Found 1 new posts.
Processing post: https://example-blog.com/example-article-2025-05-28
Done. Posts added: 1, network errors: 0, parsing errors: 0

# And collect them into a clean email newsletter
$ blogregator send-newsletter
Looking for posts from the past 8 hours...
Newsletter with 1 posts sent successfully
```

## How It Works

Blogregator uses a combination of web scraping and large language models to monitor a list of blogs for new posts. Here's a breakdown of the process:

1.  **Add a Blog**: You can add a blog to the monitoring list via a command-line interface (CLI). The tool will fetch the blog's content and use an LLM to generate a suitable scraping schema.
2.  **Scheduled Checks**: A GitHub Actions workflow runs on a schedule (daily by default) to check for new blog posts.
3.  **Scraping and Parsing**: For each blog, the system scrapes the main page and parses it to find new posts that aren't already in the database.
4.  **Content Extraction**: For each new post, it extracts the full text content and uses an LLM to generate a summary, estimate the reading time, and categorize it with relevant topics.
5.  **Email Newsletter**: If new posts are found, the system will send an email newsletter to a specified address with a summary of the new content.

## Getting Started

To set up your own instance of Blogregator, follow these steps.

### 1. Fork & Clone the Repository

First, [fork this repository](https://github.com/amanchoudhri/blogregator/fork) to your own GitHub account. Then, clone your forked repository to your local machine:

```bash
git clone https://github.com/YOUR_USERNAME/blogregator.git
cd blogregator
```

### 2. Create Environment & Install Dependencies

Create a Python virtual environment and install the required packages using `uv`.

```bash
# Create the virtual environment and install dependencies
uv sync

# Activate the virtual environment
source .venv/bin/activate
```

### 3. Set Up External Services

#### 3.1. Database

The application requires a PostgreSQL database to store information about the blogs and posts. You can set one up for free on a service like [Supabase](https://supabase.com/) or [Neon](https://neon.tech/).

#### 3.2. LLM Provider

The application uses a large language model to generate a scraping schema for new blogs and to extract metadata from posts. You can use Google Gemini, OpenAI, or another LLM provider. By default, the application uses Google Gemini.

#### 3.3. Email Service

To send email notifications, you'll need an email service that provides SMTP support. You can use a service like Gmail, SendGrid, or another transactional email provider.

### 4. Configure Environment Variables

To connect blogregator to these external services, you'll need to set several environment variables. You can set them in your shell or create a `.env` file in the project root.

```bash
# PostgreSQL database URL
DATABASE_URL="postgresql://user:password@host:port/dbname"

# LLM API key (defaults to Gemini)
GEMINI_API_KEY="your_gemini_api_key"

# Email configuration
SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="your_email@example.com"
SMTP_PASSWORD="your_email_password"
EMAIL_TO="recipient@example.com"
```

### 5. Initialize the Database

Run the `init-db` command to create all the necessary tables in your database from the `sql/schema.sql` file.

```bash
blogregator init-db
```

## Usage

Once installed and set up, you can use the `blogregator` command-line tool.

* **Add a new blog to monitor:**
    ```bash
    blogregator blog add <BLOG_URL>
    ```

* **List all monitored blogs:**
    ```bash
    blogregator blog list
    ```

* **Run a manual check for new posts:**
    ```bash
    blogregator run-check
    ```

* **Send the newsletter manually:**
    ```bash
    blogregator send-newsletter
    ```

## Automated Deployment with GitHub Actions

It's easy to deploy blogregator on Github Actions to automatically check for new blog posts.

This repository includes a pre-made workflow in `.github/workflows/blog-monitor.yml` that automatically checks for new posts and sends a newsletter on a schedule.

To use it, you must configure the environment variables from Step 3 as **Actions secrets** in your forked repository's settings (`Settings > Secrets and variables > Actions`).
