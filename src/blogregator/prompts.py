GENERATE_JSON_PROMPT = """
You are an expert web scraping assistant. Your task is to analyze the provided HTML content of a blog's main listing page and generate a JSON configuration object that can be used to extract blog post information.

The JSON configuration object must follow this schema:

{{
  "post_item_selector": "CSS_SELECTOR_FOR_EACH_POST_ITEM",
  "fields": {{
    "title": {{
      "selector": "CSS_SELECTOR_FOR_TITLE_WITHIN_POST_ITEM",
    }},
    "post_url": {{
      "selector": "CSS_SELECTOR_FOR_LINK_WITHIN_POST_ITEM",
      "base_url_handling": "relative_to_page" // or "absolute"
    }},
    "date": {{
      "selector": "CSS_SELECTOR_FOR_DATE_WITHIN_POST_ITEM",
      "attribute": "OPTIONAL_ATTRIBUTE_NAME", // e.g., "datetime" if date is in an attribute, otherwise null to get text
      "format": "STRPTIME_FORMAT_STRING" // Python strptime format string
    }}
  }}
}}

Explanation of fields:

1.  `post_item_selector`:
    *   A single, valid CSS selector that uniquely identifies the main HTML element for *each individual blog post* in a list.
    *   Example: If posts are `<li>` tags with class `blog-entry`, this might be `li.blog-entry`.

2.  `fields`: An object containing specific selectors for 'title', 'post_url', and 'date'.
    *   All CSS selectors within `fields` should be *relative to the `post_item_selector`*.

    *   `title`:
        *   `selector`: CSS selector for the element containing the post title.

    *   `post_url`:
        *   `selector`: CSS selector for the anchor (`<a>`) tag containing the link to the full post.
        *   `base_url_handling`:
            *   Set to `"relative_to_page"` if the `href` attribute contains a relative path (e.g., `/blog/my-post`, `../article.html`).
            *   Set to `"absolute"` if the `href` attribute contains a full URL (e.g., `https://example.com/blog/my-post`).

    *   `date`:
        *   `selector`: CSS selector for the element containing the publication date.
        *   `attribute`:
            *   If the date is found within an attribute of the selected element (e.g., `<time datetime="2023-01-15">...</time>`), provide the attribute name (e.g., `"datetime"`).
            *   If the date is the text content of the element, set this to `null`.
        *   `format`:
            *   Provide the Python `datetime.strptime` format string that correctly parses the date string. For example:
                *   For "January 15, 2023", use `"%B %d, %Y"`.
                *   For "15 Jan 2023 14:30", use `"%d %b %Y %H:%M"`.
                *   For "2023/12/25", use `"%Y/%m/%d"`.
            *   Ensure the format string accurately matches the date representation on the page.

General Instructions:
*   Provide robust CSS selectors. Prefer selectors that are less likely to break with minor site redesigns, but are still specific enough.
*   Ensure all CSS selectors are valid.
*   If a field (especially date) seems genuinely unavailable within the common structure of post items, try to find the most common pattern. If it's consistently absent, you can still provide selectors that would match if it were present, or make a best guess. The generic parser can handle missing elements.
*   The URL of the blog page is provided for context, which might be helpful for determining `base_url_handling` or understanding the site structure.

Blog Page URL: {blog_url}
HTML <body> content:
```html
{html_content}
```

Provide ONLY the JSON configuration object. Do not include any other text, explanations, or markdown formatting around the JSON.
"""

SCHEMA_CORRECTION_PROMPT = """
You are an expert web scraping assistant. Your task is to analyze the provided HTML content of a blog's main listing page and improve an existing JSON configuration object that can be used to extract blog post information.

The JSON configuration object must follow this schema:

{{
  "post_item_selector": "CSS_SELECTOR_FOR_EACH_POST_ITEM",
  "fields": {{
    "title": {{
      "selector": "CSS_SELECTOR_FOR_TITLE_WITHIN_POST_ITEM",
    }},
    "post_url": {{
      "selector": "CSS_SELECTOR_FOR_LINK_WITHIN_POST_ITEM",
      "base_url_handling": "relative_to_page" // or "absolute"
    }},
    "date": {{
      "selector": "CSS_SELECTOR_FOR_DATE_WITHIN_POST_ITEM",
      "attribute": "OPTIONAL_ATTRIBUTE_NAME", // e.g., "datetime" if date is in an attribute, otherwise null to get text
      "format": "STRPTIME_FORMAT_STRING" // Python strptime format string
    }}
  }}
}}

Explanation of fields:

1.  `post_item_selector`:
    *   A single, valid CSS selector that uniquely identifies the main HTML element for *each individual blog post* in a list.
    *   Example: If posts are `<li>` tags with class `blog-entry`, this might be `li.blog-entry`.

2.  `fields`: An object containing specific selectors for 'title', 'post_url', and 'date'.
    *   All CSS selectors within `fields` should be *relative to the `post_item_selector`*.

    *   `title`:
        *   `selector`: CSS selector for the element containing the post title.

    *   `post_url`:
        *   `selector`: CSS selector for the anchor (`<a>`) tag containing the link to the full post.
        *   `base_url_handling`:
            *   Set to `"relative_to_page"` if the `href` attribute contains a relative path (e.g., `/blog/my-post`, `../article.html`).
            *   Set to `"absolute"` if the `href` attribute contains a full URL (e.g., `https://example.com/blog/my-post`).

    *   `date`:
        *   `selector`: CSS selector for the element containing the publication date.
        *   `attribute`:
            *   If the date is found within an attribute of the selected element (e.g., `<time datetime="2023-01-15">...</time>`), provide the attribute name (e.g., `"datetime"`).
            *   If the date is the text content of the element, set this to `null`.
        *   `format`:
            *   Provide the Python `datetime.strptime` format string that correctly parses the date string. For example:
                *   For "January 15, 2023", use `"%B %d, %Y"`.
                *   For "15 Jan 2023 14:30", use `"%d %b %Y %H:%M"`.
                *   For "2023/12/25", use `"%Y/%m/%d"`.
            *   Ensure the format string accurately matches the date representation on the page.

General Instructions:
*   Provide robust CSS selectors. Prefer selectors that are less likely to break with minor site redesigns, but are still specific enough.
*   Ensure all CSS selectors are valid.
*   If a field (especially date) seems genuinely unavailable within the common structure of post items, try to find the most common pattern. If it's consistently absent, you can still provide selectors that would match if it were present, or make a best guess. The generic parser can handle missing elements.
*   The URL of the blog page is provided for context, which might be helpful for determining `base_url_handling` or understanding the site structure.

Previous Schema Attempt:
```json
{previous_schema}
```

Results of Previous Attempt:
{previous_results}

Blog Page URL: {blog_url}
HTML <body> content:
```html
{html_content}
```

Please analyze the previous schema and its results, and generate an improved version that better matches the actual HTML structure. Focus on:
1. Correctly identifying the post_item_selector
2. Properly locating the title, post_url, and date within each post item
3. Ensuring the selectors are robust and won't break with minor changes to the site structure

Provide ONLY the JSON configuration object. Do not include any other text, explanations, or markdown formatting around the JSON.
"""