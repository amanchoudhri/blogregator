"""
LLM utilities for Blogregator.

This module contains shared functionality for interacting with LLMs.
"""

import json
import os
import time
from typing import Any, Literal

from litellm import completion


def generate_json_from_llm(
    prompt: str,
    model: str = "gemini/gemini-3-flash-preview",
    max_retries: int = 3,
    retry_delay: float = 1.0,
    response_schema: dict[str, Any] | None = None,
    reasoning_effort: Literal["low", "medium", "high"] | None = None,
) -> dict:
    """
    Get JSON output from LLM with error handling and retries.

    Args:
        prompt: The prompt to send to the LLM.
        model: The model to use for completion.
        max_retries: Maximum number of retry attempts on failure.
        retry_delay: Delay between retry attempts in seconds.
        response_schema: Optional format specification for structured outputs.
        reasoning_effort: Optional reasoning effort level for the LLM.

    Returns:
        dict: The parsed JSON response from the LLM.

    Raises:
        ValueError: If GEMINI_API_KEY is not set.
        json.JSONDecodeError: If the response cannot be parsed as JSON.
        Exception: For other errors after all retries are exhausted.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set")

    last_error = None
    for attempt in range(max_retries):
        try:
            response_format = (
                {"type": "json_schema", "json_schema": response_schema, "strict": True}
                if response_schema
                else None
            )

            response = completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                api_key=api_key,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            )

            # Extract the generated json from the response
            result = response.choices[0].message.content  # type: ignore

            # Clean up the response - remove markdown code blocks if present
            if "```json" in result:
                result = result.split("```json")[1]
                if "```" in result:
                    result = result.split("```")[0]

            return json.loads(result)

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                print(f"LLM request attempt {attempt + 1} failed: {e}. Retrying...")
                time.sleep(retry_delay)
            continue

    # If we get here, all retries failed
    raise Exception(
        f"Failed to get valid JSON from LLM after {max_retries} attempts"
    ) from last_error
