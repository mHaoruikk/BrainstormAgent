"""
Pluggable LLM client layer.

Each pipeline agent receives an LLMClient from `make_client()`. The client's
`run_sync(prompt)` returns a RunResult whose `.output` is either:
  - an instance of the configured Pydantic output_type (structured mode), or
  - a plain string (when output_type is str).

Modes:
  - "api"   — PydanticAI Agent using API keys (original behaviour)
  - "local" — subprocess call to Claude Code or Codex CLI using your
              subscription, with JSON parsing + retry for structured output

Selection is per-agent via the `clients` section of config.yaml.
"""

from llm_clients.base import LLMClient, RunResult
from llm_clients.factory import make_client

__all__ = ["LLMClient", "RunResult", "make_client"]
