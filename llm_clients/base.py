"""
Client protocol and shared result type.

The `.output` attribute on RunResult intentionally mirrors PydanticAI's
`AgentRunResult.output` so agent call sites (`client.run_sync(msg).output`)
don't care which backend produced the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class RunResult:
    output: Any


@runtime_checkable
class LLMClient(Protocol):
    def run_sync(self, prompt: str) -> RunResult: ...
