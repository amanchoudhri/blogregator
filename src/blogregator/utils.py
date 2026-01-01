import datetime
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

from playwright.sync_api import sync_playwright


def utcnow():
    return datetime.datetime.now(datetime.UTC)


@dataclass
class FetchResponse:
    """Response object mimicking requests.Response for compatibility."""

    content: bytes
    text: str
    status_code: int
    url: str

    def raise_for_status(self):
        """Raise an exception if status code indicates an error."""
        if self.status_code >= 400:
            raise FetchError(f"HTTP {self.status_code} error for URL: {self.url}")


class FetchError(Exception):
    """Exception raised when fetching a URL fails."""

    pass


def fetch_with_retries(url: str, retries: int = 3, sleep: int = 1) -> FetchResponse:
    """
    Fetch HTML content from a URL using Playwright, with retry logic.

    Uses a headless Chromium browser to render JavaScript and handle
    dynamic content that simple HTTP requests cannot capture.

    Args:
        url: The URL to fetch
        retries: Number of retry attempts (default: 3)
        sleep: Seconds to wait between retries (default: 1)

    Returns:
        FetchResponse object with content, text, status_code, and url attributes

    Raises:
        FetchError: If all retry attempts fail
    """
    last_error = None

    for attempt in range(retries):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                # Navigate to the URL and wait for network to be idle
                response = page.goto(url, wait_until="networkidle", timeout=30000)

                if response is None:
                    raise FetchError(f"No response received for URL: {url}")

                status_code = response.status

                # Get the fully rendered HTML content
                html_content = page.content()

                browser.close()

                result = FetchResponse(
                    content=html_content.encode("utf-8"),
                    text=html_content,
                    status_code=status_code,
                    url=url,
                )

                # Check for HTTP errors
                result.raise_for_status()

                return result

        except Exception as e:
            last_error = e
            print(f"Error fetching the URL (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(sleep)

    raise FetchError(f"Unable to retrieve content from page: {url}. Last error: {last_error}")


def multiline_user_input(initial_message: str = ""):
    """
    Opens an editor and returns the user's input.

    Args:
        initial_message: The initial message to display in the editor.

    Returns:
        The user's input as a string, or None if an error occurs.
    """
    editor = os.environ.get("EDITOR", "nano")

    with tempfile.NamedTemporaryFile("w+", suffix=".tmp", delete=False) as tf:
        if initial_message:
            tf.write(initial_message)
            tf.flush()
        temp_filename = tf.name
    try:
        subprocess.call((editor, temp_filename))
        with open(temp_filename) as f:
            return f.read()
    except Exception as e:
        print(f"Error opening editor: {e}")
        return None
    finally:
        os.remove(temp_filename)
