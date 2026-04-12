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
from pydantic_ai import Agent

from config import config, settings

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
    # PydanticAI Agent factory
    # ------------------------------------------------------------------

    def make_agent(
        self,
        model: str,
        output_type: type[T],
        extra_prompt: str = "",
        retries: int = 3,
    ) -> Agent:
        """
        Create a PydanticAI Agent with the assembled system prompt.

        Args:
            model: PydanticAI model string, e.g. "openai:gpt-4o"
            output_type: Pydantic model class for structured output
            extra_prompt: Additional instructions appended to the system prompt
            retries: Max validation retries (default 3)
        """
        return Agent(
            model=model,
            output_type=output_type,
            system_prompt=self.build_system_prompt(extra=extra_prompt),
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
            decision = ex.get("decision", "")
            reason = ex.get("reason", ex.get("question", ""))
            lines.append(f"- **{title}** → {decision}: {reason}")
        return "\n".join(lines)
