import time
import datetime

import requests

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

def fetch_with_retries(url, retries=3, sleep=1):
    """Fetch the HTML content from a URL, with retry logic."""
    attempts = 0
    while attempts < retries:
        try:
            response = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            response.raise_for_status()
            return response

        except requests.RequestException as e:
            print(f"Error fetching the URL: {e}")
            attempts += 1
            time.sleep(sleep)
    raise requests.RequestException(f'Unable to retrieve content from page: {url}')