"""
BaseAgent: shared foundation for all pipeline agents.

Responsibilities:
- Load the shared research direction from instructions/research_direction.yaml
- Load per-agent instruction files from instructions/
- Build a formatted system prompt combining both
- Provide a factory method for PydanticAI Agent instances
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from config import config, settings
from llm_clients import LLMClient, make_client

T = TypeVar("T", bound=BaseModel)

_INSTRUCTIONS_DIR = Path(__file__).parent.parent / "instructions"


class BaseAgent:
    """
    All pipeline agents subclass this.

    Subclass usage:
        class MyAgent(BaseAgent):
            def __init__(self, notion):
                super().__init__(instruction_file="my_agent.yaml")
                self._agent = self.make_agent("openai:gpt-4o", MyResultModel)
    """

    def __init__(self, instruction_file: str | None = None) -> None:
        self.research_direction = self._load_yaml("research_direction.yaml")
        self.instructions: dict = {}
        if instruction_file:
            self.instructions = self._load_yaml(instruction_file)

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def build_system_prompt(self, extra: str = "") -> str:
        """
        Assemble a system prompt from:
        1. Research direction (shared across all agents)
        2. Agent-specific instructions (from the instruction YAML)
        3. Optional extra text (per-call additions)
        """
        parts: list[str] = []

        # Research context
        rd = self.research_direction
        parts.append(f"## Research Direction\n{rd.get('description', '').strip()}")
        parts.append(f"## Researcher Profile\n{rd.get('researcher_profile', '').strip()}")

        goals = rd.get("goals", [])
        if goals:
            parts.append("## Research Goals\n" + "\n".join(f"- {g}" for g in goals))

        topics = rd.get("topics", [])
        if topics:
            parts.append("## Priority Topics\n" + "\n".join(f"- {t}" for t in topics))

        non_goals = rd.get("non_goals", [])
        if non_goals:
            parts.append("## Non-Goals\n" + "\n".join(f"- {g}" for g in non_goals))

        # Agent-specific instructions
        if self.instructions:
            role = self.instructions.get("role", "")
            if role:
                parts.append(f"## Your Role\n{role.strip()}")

            criteria = self.instructions.get("criteria", {})
            if criteria:
                parts.append("## Criteria\n" + self._format_criteria(criteria))

            output_instructions = self.instructions.get("output_instructions", "")
            if output_instructions:
                parts.append(f"## Output Instructions\n{output_instructions.strip()}")

            examples = self.instructions.get("examples", [])
            if examples:
                parts.append("## Examples\n" + self._format_examples(examples))

        if extra:
            parts.append(extra)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # LLM client factory (mode-aware: api vs local subscription CLI)
    # ------------------------------------------------------------------

    def make_client(
        self,
        agent_key: str,
        output_type: type[T],
        extra_prompt: str = "",
        *,
        index: int | None = None,
        model_settings: Any = None,
        retries: int = 3,
    ) -> LLMClient:
        """
        Return an LLMClient wired to the backend selected in config.clients.<agent_key>.

        Usage mirrors the previous PydanticAI agent contract — call
        `client.run_sync(msg).output` to get the typed result.

        Args:
            agent_key: matches a `clients.<key>` entry in config.yaml.
                       Use `index` for list-configured agents ("brainstorm", "critic").
            output_type: Pydantic model class for structured output, or `str`
                         for free-text agents.
            extra_prompt: appended to the assembled system prompt.
            index: required for "brainstorm" / "critic"; must be None otherwise.
            model_settings: forwarded to PydanticAI in api mode (ignored in local mode).
            retries: validation/retry budget.
        """
        return make_client(
            agent_key=agent_key,
            output_type=output_type,
            system_prompt=self.build_system_prompt(extra=extra_prompt),
            index=index,
            model_settings=model_settings,
            retries=retries,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_yaml(filename: str) -> dict:
        path = _INSTRUCTIONS_DIR / filename
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _format_criteria(criteria: dict) -> str:
        lines: list[str] = []
        for section, items in criteria.items():
            lines.append(f"**{section.replace('_', ' ').title()}**")
            if isinstance(items, list):
                lines.extend(f"  - {item}" for item in items)
            else:
                lines.append(f"  {items}")
        return "\n".join(lines)

    @staticmethod
    def _format_examples(examples: list[dict]) -> str:
        lines: list[str] = []
        for ex in examples:
            title = ex.get("title", "")
            full_proposal = ex.get("full_proposal", "")
            source_paper = ex.get("source_paper", "")
            if full_proposal:
                lines.append(f"### {title}")
                if source_paper:
                    lines.append(f"Source paper: {source_paper}")
                lines.append(full_proposal)
                continue

            decision = ex.get("decision", "")
            reason = ex.get("reason", ex.get("question", ""))
            lines.append(f"- **{title}** → {decision}: {reason}")
        return "\n".join(lines)
