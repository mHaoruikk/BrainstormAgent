"""
API-mode client — thin wrapper around pydantic_ai.Agent.

Preserves the original PydanticAI behaviour (structured-output tool calls,
automatic validation retries, optional model_settings like reasoning_effort).
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from llm_clients.base import RunResult


class APIClient:
    def __init__(
        self,
        model: str,
        output_type: type,
        system_prompt: str,
        retries: int = 3,
        model_settings: Any = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "model": model,
            "output_type": output_type,
            "system_prompt": system_prompt,
            "retries": retries,
        }
        if model_settings is not None:
            kwargs["model_settings"] = model_settings
        self._agent = Agent(**kwargs)

    def run_sync(self, prompt: str) -> RunResult:
        result = self._agent.run_sync(prompt)
        return RunResult(output=result.output)
