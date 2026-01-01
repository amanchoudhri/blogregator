import datetime
import os
import subprocess
import tempfile
import time

import requests


def utcnow():
    return datetime.datetime.now(datetime.UTC)


def fetch_with_retries(url, retries=3, sleep=1):
    """Fetch the HTML content from a URL, with retry logic."""
    attempts = 0
    while attempts < retries:
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                },
            )
            response.raise_for_status()
            return response

        except requests.RequestException as e:
            print(f"Error fetching the URL: {e}")
            attempts += 1
            time.sleep(sleep)
    raise requests.RequestException(f"Unable to retrieve content from page: {url}")


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
