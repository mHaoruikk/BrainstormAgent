"""
Structured-output coercion for subscription-CLI backends.

Claude Code and Codex CLIs return free-form text, so we:
  1. Append the Pydantic JSON schema and a strict formatting rule to the prompt.
  2. Strip common wrapper patterns (``` fences, stray prose) from the reply.
  3. Validate via model_validate_json; on failure feed the error back to the
     model and retry up to `max_retries` times.

For output_type == str we skip parsing and return the raw text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def build_json_prompt(user_prompt: str, output_type: type) -> str:
    """Return user_prompt with a strict JSON-output preamble appended."""
    if output_type is str:
        return user_prompt

    schema = _compact_schema(output_type)
    return (
        f"{user_prompt}\n\n"
        "---\n"
        "OUTPUT FORMAT (strict):\n"
        "Respond with a single JSON object that conforms exactly to this schema.\n"
        "Do not wrap it in markdown code fences. Do not include any commentary, "
        "preamble, or trailing text. Output the JSON and nothing else.\n\n"
        f"JSON Schema:\n{schema}\n"
    )


def parse_output(
    raw_text: str,
    output_type: type,
    *,
    retry: Callable[[str], str] | None = None,
    max_retries: int = 2,
) -> Any:
    """
    Coerce a CLI response into output_type.

    If validation fails and `retry` is provided, the validation error is sent
    back to the model via retry(error_feedback_prompt), up to max_retries.
    """
    if output_type is str:
        return _strip_fences(raw_text)

    attempt = 0
    text = raw_text
    last_error: Exception | None = None

    while True:
        candidate = _extract_json(text)
        try:
            return output_type.model_validate_json(candidate)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if retry is None or attempt >= max_retries:
                raise RuntimeError(
                    f"Local LLM returned output that failed {output_type.__name__} "
                    f"validation after {attempt} retry/retries: {exc}\n"
                    f"--- Raw output (first 500 chars) ---\n{raw_text[:500]}"
                ) from exc
            attempt += 1
            feedback = (
                "Your previous response did not validate against the required schema.\n"
                f"Validation error:\n{exc}\n\n"
                "Return ONLY a corrected JSON object matching the schema. "
                "No markdown, no commentary."
            )
            text = retry(feedback)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _extract_json(text: str) -> str:
    """
    Pull the first top-level JSON object out of a free-form reply.

    Handles: raw JSON, fenced JSON, and JSON embedded in prose. Falls back to
    the cleaned text if no braces are found so the downstream parser produces
    a useful error message.
    """
    cleaned = _strip_fences(text)
    start = cleaned.find("{")
    if start < 0:
        return cleaned

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    return cleaned[start:]


def _compact_schema(output_type: type) -> str:
    """Return a human-readable JSON schema without Pydantic metadata bloat."""
    if not (isinstance(output_type, type) and issubclass(output_type, BaseModel)):
        return "<any JSON object>"
    schema = output_type.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False)
