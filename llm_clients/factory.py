"""
make_client() — resolves per-agent client config and returns an LLMClient.

Agent keys (match `config.yaml` entries):
    "ingestion" | "filter" | "summary" | "proposal_writer"   → scalar config
    "brainstorm" | "critic"                                  → list config
                                                               (requires `index`)

`mode: api`  returns an APIClient (uses the API-model string from config.models.<agent>
             and respects model_settings).
`mode: local` inspects local_model to pick Claude vs Codex:
    "claude:<name>" | "anthropic:<name>"  → ClaudeCodeClient(model=<name>)
    "openai:<name>" | "gpt*" | "o*"       → CodexClient(model=<name>)
"""

from __future__ import annotations

from typing import Any

from config import config
from llm_clients.api_client import APIClient
from llm_clients.base import LLMClient
from llm_clients.claude_code_client import ClaudeCodeClient
from llm_clients.codex_client import CodexClient


_LIST_AGENTS = {"brainstorm", "critic"}


def make_client(
    agent_key: str,
    output_type: type,
    system_prompt: str,
    *,
    index: int | None = None,
    model_settings: Any = None,
    retries: int = 3,
) -> LLMClient:
    client_cfg = _resolve_client_cfg(agent_key, index)
    mode = client_cfg.mode.lower()

    if mode == "api":
        model = _resolve_api_model(agent_key, index)
        return APIClient(
            model=model,
            output_type=output_type,
            system_prompt=system_prompt,
            retries=retries,
            model_settings=model_settings,
        )

    if mode == "local":
        provider, model_name = _split_local_model(client_cfg.local_model)
        if provider == "claude":
            return ClaudeCodeClient(
                model=model_name,
                output_type=output_type,
                system_prompt=system_prompt,
                max_retries=retries,
            )
        if provider == "openai":
            return CodexClient(
                model=model_name,
                output_type=output_type,
                system_prompt=system_prompt,
                max_retries=retries,
            )
        raise ValueError(
            f"Unknown local_model provider '{provider}' for agent '{agent_key}'. "
            f"Prefix with 'claude:' or 'openai:' (or use anthropic:/gpt-*/o*)."
        )

    raise ValueError(f"Unknown client mode '{mode}' for agent '{agent_key}'")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_client_cfg(agent_key: str, index: int | None):
    clients = config.clients
    if agent_key in _LIST_AGENTS:
        entries = getattr(clients, agent_key)
        if index is None:
            raise ValueError(f"`index` is required for list-configured agent '{agent_key}'")
        if index < 0 or index >= len(entries):
            raise IndexError(
                f"client index {index} out of range for '{agent_key}' "
                f"(len={len(entries)})"
            )
        return entries[index]

    if index is not None:
        raise ValueError(f"`index` must be None for scalar agent '{agent_key}'")
    if not hasattr(clients, agent_key):
        raise ValueError(f"No client config for agent '{agent_key}'")
    return getattr(clients, agent_key)


def _resolve_api_model(agent_key: str, index: int | None) -> str:
    models = config.models
    value = getattr(models, agent_key)
    if agent_key in _LIST_AGENTS:
        return value[index].model  # type: ignore[index]
    return value  # type: ignore[return-value]


def _split_local_model(spec: str) -> tuple[str, str]:
    """
    Parse 'claude:opus' → ('claude', 'opus'), 'openai:gpt-5' → ('openai', 'gpt-5').

    Also accepts 'anthropic:<name>' (→ claude) and bare model names starting
    with 'gpt' or 'o' (→ openai) to keep config terse.
    """
    spec = spec.strip()
    if ":" in spec:
        prefix, name = spec.split(":", 1)
        prefix = prefix.lower().strip()
        name = name.strip()
        if prefix in ("claude", "anthropic"):
            return "claude", name
        if prefix in ("openai", "gpt"):
            return "openai", name
        return prefix, name  # unknown — surfaces as error upstream

    lowered = spec.lower()
    if lowered.startswith(("gpt", "o1", "o3", "o4", "o5")):
        return "openai", spec
    if lowered in ("opus", "sonnet", "haiku") or lowered.startswith("claude"):
        return "claude", spec
    # Default to claude because it's the one we can verify is installed.
    return "claude", spec
