import datetime

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Any, Optional

def parse_post_list(page_url: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts blog post data from a given URL using a JSON configuration object.

    Args:
        page_url (str): URL of the blog's main/listing page.
        config (Dict[str, Any]): A JSON-like dictionary defining the selectors:
            {
              "post_item_selector": "CSS_SELECTOR_FOR_EACH_POST_ITEM",
              "fields": {
                "title": {"selector": "CSS_SELECTOR", "type": "text"},
                "post_url": {"selector": "CSS_SELECTOR", "attribute": "href", "base_url_handling": "relative_to_page"},
                "date": {"selector": "CSS_SELECTOR", "attribute": "datetime" or None, "type": "date_string" or "date_iso"}
              }
            }

    Returns:
        List[Dict[str, Any]]: A list of dictionaries, where each dictionary
        contains 'title', 'post_url', and 'date' for a post.
        Returns an empty list if fetching fails or no posts are found.
    """
    try:
        response = requests.get(page_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching the URL {page_url}: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    
    post_item_selector = config.get("post_item_selector")
    if not post_item_selector:
        print(f"Error: 'post_item_selector' not found in config for {page_url}.")
        return []

    fields_config = config.get("fields")
    if not fields_config:
        print(f"Error: 'fields' not found in config for {page_url}.")
        return []

    # import pdb; pdb.set_trace()
    post_elements = soup.select(post_item_selector)
    if not post_elements:
        print(f"No post elements found using selector '{post_item_selector}' on {page_url}.")
        return []

    results: List[Dict[str, Any]] = []
    for post_element in post_elements:
        post_data: Dict[str, Optional[str]] = {
            "title": None,
            "post_url": None,
            "date": None
        }
        for field_name, field_spec in fields_config.items():
            if field_name not in post_data: # Only process 'title', 'post_url', 'date'
                continue

            item_selector = field_spec.get("selector")
            if not item_selector:
                print(f"Warning: Missing 'selector' for field '{field_name}' in config for {page_url}.")
                continue

            target_element = post_element.select_one(item_selector)
            if not target_element:
                # It's common for some fields (e.g. date) to sometimes be missing for a post
                # print(f"Warning: Element for field '{field_name}' with selector '{item_selector}' not found in a post item on {page_url}.")
                continue

            value: Optional[str] = None
            attribute_name = field_spec.get("attribute")
            if field_name == 'post_url' and attribute_name is None:
                attribute_name = "href"

            if attribute_name:
                value = target_element.get(attribute_name)
            else:
                value = target_element.get_text(strip=True)
            
            if value is not None: # Ensure value was actually extracted
                if field_name == "post_url":
                    if field_spec.get("base_url_handling") == "relative_to_page" and \
                       value and not value.startswith(('http://', 'https://', '#')):
                        value = urljoin(page_url, value)
                if field_name == "date":
                    value = datetime.datetime.strptime(value, field_spec['format']).isoformat()
                post_data[field_name] = value
            
        # Basic validation: ensure we have at least a title and URL for it to be a useful entry
        if post_data.get("title") and post_data.get("post_url"):
            results.append(post_data) # type: ignore 
            # We initialized with None, but now we're appending a dict that might still have Nones
            # but the important ones (title, post_url) are checked.
        else:
            print(f"Skipping a post item from {page_url} due to missing title or URL. Data: {post_data}")
            
    return results