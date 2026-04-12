"""
Brainstorm Pipeline.

Orchestrates one or more multi-round brainstorm/critique dialogues for papers
that passed the initial filter.

Each configured brainstorm model is paired with each configured critic model.
Every pair runs an independent multi-round refinement loop and writes a
combined round page to Notion.

The loop terminates when:
  - The final critique meets the configured novelty/viability thresholds, OR
  - max_rounds is reached.

After the final round, aggregate scores are written to the paper's DB properties
using the strongest final critique across all configured model pairs.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pypdf import PdfReader
from pydantic import BaseModel
from rich.console import Console

from agents.base import BaseAgent
from config import config

_console = Console()
from models.paper import BrainstormResult, CritiqueResult
from notion.client import NotionClient
from notion.schema import (
    ChildPages,
    Props,
    Status,
    prop_number,
    prop_select,
    prop_text,
)


# ---------------------------------------------------------------------------
# Per-paper run outcome (returned to CLI)
# ---------------------------------------------------------------------------

class BrainstormRunResult(BaseModel):
    """Outcome of running the brainstorm pipeline on a single paper."""
    paper_id: str
    title: str
    rounds_completed: int
    final_recommendation: str
    status: Literal["ok", "error", "skipped"]
    message: str


@dataclass
class _AgentRunner:
    label: str
    model: str
    agent: object


@dataclass
class _PairRun:
    brainstorm_label: str
    critic_label: str
    rounds: list[tuple[BrainstormResult, CritiqueResult]]

    @property
    def final_critique(self) -> CritiqueResult:
        return self.rounds[-1][1]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class BrainstormPipeline(BaseAgent):
    """
    Owns the brainstorm and critique PydanticAI agents and the
    multi-round loop that connects them.
    """

    def __init__(self, notion: NotionClient) -> None:
        super().__init__(instruction_file="brainstorm_agent.yaml")
        self.notion = notion
        self._max_rounds: int = config.brainstorm.max_rounds

        if not config.models.brainstorm:
            raise ValueError("config.models.brainstorm must contain at least one model")
        if not config.models.critic:
            raise ValueError("config.models.critic must contain at least one model")

        self._brainstorm_agents = [
            _AgentRunner(
                label=model_cfg.label,
                model=model_cfg.model,
                agent=self.make_agent(
                    model=model_cfg.model,
                    output_type=BrainstormResult,
                    extra_prompt=self._build_brainstorm_extra(),
                ),
            )
            for model_cfg in config.models.brainstorm
        ]

        self._critic_base = BaseAgent(instruction_file="critic_agent.yaml")
        self._critic_agents = [
            _AgentRunner(
                label=model_cfg.label,
                model=model_cfg.model,
                agent=self._critic_base.make_agent(
                    model=model_cfg.model,
                    output_type=CritiqueResult,
                    extra_prompt=self._build_critic_extra(),
                ),
            )
            for model_cfg in config.models.critic
        ]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        paper_id: str | None = None,
        rerun: bool = False,
    ) -> list[BrainstormRunResult]:
        """
        Run brainstorming on eligible papers.

        rerun=True also accepts Critiqued / Needs Review papers and injects
        all previous round pages as compressed context for the new session.
        """
        eligible = [Status.FILTER_PASS, Status.BRAINSTORMING]
        if rerun:
            eligible += [Status.CRITIQUED, Status.NEEDS_REVIEW]

        if paper_id:
            page = self.notion.get_paper_by_paper_id(paper_id)
            if page is None:
                return [BrainstormRunResult(
                    paper_id=paper_id, title="", rounds_completed=0,
                    final_recommendation="",
                    status="error", message=f"Paper not found: {paper_id}",
                )]
            pages = [page]
        else:
            pages = self.notion.query_papers_multi_status(eligible)

        results: list[BrainstormRunResult] = []
        for page in pages:
            result = self._run_pipeline(page, rerun=rerun)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Core multi-round loop for a single paper
    # ------------------------------------------------------------------

    def _run_pipeline(self, page: dict, rerun: bool = False) -> BrainstormRunResult:
        props = page["properties"]
        page_id = page["id"]
        title = _get_title(props)
        paper_id = _get_text(props, Props.PAPER_ID)

        current_status = (props.get(Props.STATUS, {}).get("select") or {}).get("name", "")
        eligible = [Status.FILTER_PASS, Status.BRAINSTORMING]
        if rerun:
            eligible += [Status.CRITIQUED, Status.NEEDS_REVIEW]

        if current_status not in eligible:
            return BrainstormRunResult(
                paper_id=paper_id, title=title, rounds_completed=0,
                final_recommendation="",
                status="skipped",
                message=(
                    f"Skipping '{title}': status is {current_status}. "
                    f"Use --rerun to re-brainstorm Critiqued/Needs Review papers."
                ),
            )

        # Set status to Brainstorming
        self.notion.update_paper(page_id, {
            Props.STATUS: prop_select(Status.BRAINSTORMING),
        })

        try:
            # Load prior round pages as compressed context when re-running
            prior_context, round_offset = self._load_prior_context(page_id) if rerun else ("", 0)

            if prior_context:
                _console.print(
                    f"  [dim]Loaded prior context:[/dim] {round_offset} round(s), "
                    f"~{_estimate_tokens(prior_context):,} tokens"
                )

            # Gather paper context from Notion
            paper_context = self._gather_paper_context(page_id, props, prior_context=prior_context)
            pair_runs: list[_PairRun] = []

            for brainstorm_runner in self._brainstorm_agents:
                for critic_runner in self._critic_agents:
                    pair_runs.append(
                        self._run_pair(
                            page_id=page_id,
                            paper_context=paper_context,
                            brainstorm_runner=brainstorm_runner,
                            critic_runner=critic_runner,
                            round_offset=round_offset,
                        )
                    )

            best_pair = max(pair_runs, key=self._pair_rank_key)
            final_critique = best_pair.final_critique
            self._write_final_scores(page_id, final_critique)

            log_msg = (
                f"Brainstorm: '{title}' → {final_critique.recommendation} "
                f"after {len(best_pair.rounds)} round(s) "
                f"[best pair: {best_pair.brainstorm_label} + {best_pair.critic_label}] "
                f"(novelty={final_critique.novelty_score}, "
                f"viability={final_critique.viability_score})"
            )

            try:
                self.notion.append_to_log(log_msg)
            except Exception:
                pass

            return BrainstormRunResult(
                paper_id=paper_id,
                title=title,
                rounds_completed=len(best_pair.rounds),
                final_recommendation=final_critique.recommendation,
                status="ok",
                message=log_msg,
            )

        except Exception as exc:
            return BrainstormRunResult(
                paper_id=paper_id, title=title, rounds_completed=0,
                final_recommendation="",
                status="error",
                message=f"Error brainstorming '{title}': {exc}",
            )

    def _run_pair(
        self,
        page_id: str,
        paper_context: str,
        brainstorm_runner: _AgentRunner,
        critic_runner: _AgentRunner,
        round_offset: int = 0,
    ) -> _PairRun:
        rounds: list[tuple[BrainstormResult, CritiqueResult]] = []
        max_tokens = config.brainstorm.max_input_tokens

        for round_num in range(1, self._max_rounds + 1):
            page_round_num = round_num + round_offset  # global round number for Notion page titles
            brainstorm_msg = self._build_brainstorm_message(
                paper_context, rounds, round_num
            )
            brainstorm_msg = self._enforce_token_limit(
                brainstorm_msg, max_tokens, f"brainstorm round {round_num}"
            )
            b_tokens = _estimate_tokens(brainstorm_msg)
            _console.print(
                f"  [dim]Round {page_round_num} brainstorm[/dim] "
                f"[cyan]{brainstorm_runner.label}[/cyan]: "
                f"[bold]{b_tokens:,}[/bold] tokens"
            )

            try:
                brainstorm_result: BrainstormResult = (
                    brainstorm_runner.agent.run_sync(brainstorm_msg).output
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Brainstorm agent ({brainstorm_runner.label}) failed on round {page_round_num}: {exc}"
                ) from exc

            critique_msg = self._build_critique_message(
                brainstorm_result, rounds, round_num
            )
            critique_msg = self._enforce_token_limit(
                critique_msg, max_tokens, f"critic round {page_round_num}"
            )
            c_tokens = _estimate_tokens(critique_msg)
            _console.print(
                f"  [dim]Round {page_round_num} critic[/dim]    "
                f"[cyan]{critic_runner.label}[/cyan]: "
                f"[bold]{c_tokens:,}[/bold] tokens"
            )

            try:
                critique_result: CritiqueResult = (
                    critic_runner.agent.run_sync(critique_msg).output
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Critic agent ({critic_runner.label}) failed on round {page_round_num}: {exc}"
                ) from exc

            rounds.append((brainstorm_result, critique_result))

            round_page_content = _format_round_page(
                round_num=page_round_num,
                brainstorm=brainstorm_result,
                critique=critique_result,
                brainstorm_label=brainstorm_runner.label,
                brainstorm_model=brainstorm_runner.model,
                critic_label=critic_runner.label,
                critic_model=critic_runner.model,
            )
            self.notion.create_child_page(
                parent_page_id=page_id,
                title=ChildPages.brainstorm_round(
                    page_round_num,
                    brainstorm_label=brainstorm_runner.label,
                    critic_label=critic_runner.label,
                ),
                markdown=round_page_content,
            )

            if self._meets_threshold(critique_result):
                break

        return _PairRun(
            brainstorm_label=brainstorm_runner.label,
            critic_label=critic_runner.label,
            rounds=rounds,
        )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    @staticmethod
    def _enforce_token_limit(msg: str, max_tokens: int, label: str) -> str:
        """
        If the message exceeds max_tokens, compact it by:
        1. Truncating the "Full Paper Text" section first (biggest chunk).
        2. If still over, truncating older round histories.
        3. As a last resort, hard-truncating the entire message.
        """
        tokens = _estimate_tokens(msg)
        if tokens <= max_tokens:
            return msg

        _console.print(
            f"  [yellow]⚠ {label}: {tokens:,} tokens exceeds limit "
            f"({max_tokens:,}). Compacting...[/yellow]"
        )

        # Step 1: Truncate the full paper text section
        marker = "\n## Full Paper Text\n"
        if marker in msg:
            before, after = msg.split(marker, 1)
            # Find the next section header
            next_section = _find_next_section(after)
            if next_section >= 0:
                paper_text = after[:next_section]
                remainder = after[next_section:]
            else:
                paper_text = after
                remainder = ""

            # Progressively shrink paper text
            for fraction in (0.5, 0.25, 0.1, 0.0):
                if fraction > 0:
                    keep_chars = int(len(paper_text) * fraction)
                    truncated_paper = paper_text[:keep_chars] + "\n\n[... truncated for token limit ...]\n"
                else:
                    truncated_paper = "[Full paper text omitted for token limit.]\n"
                msg = before + marker + truncated_paper + remainder
                if _estimate_tokens(msg) <= max_tokens:
                    return msg

        # Step 2: Compact older round histories — keep only the last round
        for round_marker in ("## Round 1 — Your Proposal", "## Round 1 — Critique"):
            if round_marker in msg:
                idx = msg.find(round_marker)
                # Find the start of the last round section
                last_task_marker = "## Your Task for Round"
                task_idx = msg.rfind(last_task_marker)
                if task_idx > idx:
                    # Keep everything before the rounds and from the last task onward
                    rounds_start = idx
                    compact_note = "[... earlier rounds omitted for token limit ...]\n\n"
                    msg = msg[:rounds_start] + compact_note + msg[task_idx:]
                    if _estimate_tokens(msg) <= max_tokens:
                        return msg

        # Step 3: Hard truncate
        max_chars = max_tokens * 4  # ~4 chars per token
        msg = msg[:max_chars] + "\n\n[... hard-truncated for token limit ...]"
        return msg

    # ------------------------------------------------------------------
    # Paper context gathering
    # ------------------------------------------------------------------

    def _gather_paper_context(
        self,
        page_id: str,
        props: dict,
        prior_context: str = "",
    ) -> str:
        """
        Build a text block with the paper's content for the brainstorm agent.
        Reads full paper text (from PDF) + abstract + Math Summary + Filter Report.
        When re-running, prior_context is prepended so the model avoids
        reproducing angles already explored.
        """
        title = _get_title(props)
        abstract = _get_text(props, Props.ABSTRACT)

        parts = [
            f"# {title}",
            f"\n## Abstract\n{abstract}",
        ]

        # Prior brainstorm sessions (injected for reruns)
        if prior_context:
            parts.append(f"\n## Prior Brainstorm Sessions\n{prior_context}")

        # Full paper text from PDF attachment (controlled by config switch)
        if config.brainstorm.include_pdf:
            full_text = self._fetch_paper_text(page_id, props)
            if full_text:
                parts.append(f"\n## Full Paper Text\n{full_text}")

        # Math Summary (written by filter agent)
        math_summary = self.notion.get_child_page_text_by_title(
            page_id, ChildPages.MATH_SUMMARY
        )
        if math_summary:
            parts.append(f"\n## Math Summary\n{math_summary}")

        # Filter Report (causal relevance assessment)
        filter_report = self.notion.get_child_page_text_by_title(
            page_id, ChildPages.FILTER_REPORT
        )
        if filter_report:
            parts.append(f"\n## Filter Assessment\n{filter_report}")

        return "\n".join(parts)

    def _load_prior_context(self, page_id: str) -> tuple[str, int]:
        """
        Fetch all existing brainstorm round child pages and compress them into
        a single text block.

        Returns (compressed_text, round_count) where round_count is the number
        of prior rounds found (used to offset new round numbering).
        """
        try:
            resp = self.notion._http.get(f"/blocks/{page_id}/children")
            resp.raise_for_status()
            blocks = resp.json().get("results", [])
        except Exception:
            return "", 0

        # Collect child pages whose title matches the brainstorm round pattern
        import re as _re_local
        round_pattern = _re_local.compile(r"Brainstorm\s*[—-].*Round\s*(\d+)", _re_local.IGNORECASE)

        round_pages: list[tuple[int, str, str]] = []  # (round_num, page_id, title)
        for block in blocks:
            if block.get("type") != "child_page":
                continue
            block_title = block["child_page"]["title"]
            m = round_pattern.search(block_title)
            if m:
                round_pages.append((int(m.group(1)), block["id"], block_title))

        if not round_pages:
            return "", 0

        round_pages.sort(key=lambda x: x[0])
        max_round = round_pages[-1][0]

        # Fetch text for each round page and build compressed context
        parts: list[str] = []
        for _, block_id, block_title in round_pages:
            text = self.notion.get_child_page_text(block_id)
            if text:
                parts.append(f"### {block_title}\n{text}")

        compressed = "\n\n---\n\n".join(parts)
        return compressed, max_round

    def _fetch_paper_text(self, page_id: str, props: dict) -> str | None:
        """
        Download the PDF attached to the Notion page and extract its text.
        Falls back to the PDF URL / ArXiv URL stored in page properties.
        """
        # 1. Try the PDF block attached to the page
        url = self.notion.get_pdf_url_from_page(page_id)
        # 2. Fall back to property URLs
        if not url:
            url = _get_url(props, Props.PDF_URL) or _get_url(props, Props.ARXIV_URL)
        if not url:
            return None
        return _download_and_extract(url, max_pages=10)

    # ------------------------------------------------------------------
    # User message builders
    # ------------------------------------------------------------------

    def _build_brainstorm_message(
        self,
        paper_context: str,
        prior_rounds: list[tuple[BrainstormResult, CritiqueResult]],
        round_num: int,
    ) -> str:
        """Build the user message for the brainstorm agent."""
        parts = [paper_context]

        if prior_rounds:
            parts.append("\n---\n")
            for i, (br, cr) in enumerate(prior_rounds, 1):
                parts.append(f"## Round {i} — Your Proposal\n{br.full_report}")
                parts.append(f"\n## Round {i} — Critique\n{cr.full_report}")

            parts.append(f"\n---\n## Your Task for Round {round_num}")
            parts.append(
                "The critique above identified weaknesses. Refine your proposal "
                "addressing the feedback. Show explicitly what changed and why. "
                "Follow the refinement instructions in your system prompt."
            )
        else:
            parts.append(
                "\n---\n\nPropose ONE research angle based on this paper. "
                "Follow the standard proposal format in your system prompt."
            )

        return "\n".join(parts)

    def _build_critique_message(
        self,
        brainstorm_result: BrainstormResult,
        prior_rounds: list[tuple[BrainstormResult, CritiqueResult]],
        round_num: int,
    ) -> str:
        """Build the user message for the critique agent."""
        parts = [f"## Proposal (Round {round_num})\n{brainstorm_result.full_report}"]

        if prior_rounds:
            parts.append("\n---\n")
            prev_cr = prior_rounds[-1][1]
            parts.append(
                f"## Prior Critique (Round {round_num - 1})\n{prev_cr.full_report}"
            )
            parts.append(
                f"\n---\n## Your Task for Round {round_num}\n"
                "Re-assess the revised proposal above. Note which weaknesses from "
                "the prior critique have been addressed and which remain. Follow "
                "the refinement instructions in your system prompt."
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Final DB writes
    # ------------------------------------------------------------------

    def _write_final_scores(
        self,
        page_id: str,
        critique: CritiqueResult,
    ) -> None:
        """Write aggregate scores and advance status after the final round."""
        new_status = Status.CRITIQUED if self._meets_threshold(critique) else Status.NEEDS_REVIEW

        self.notion.update_paper(page_id, {
            Props.NOVELTY_RATING: prop_number(critique.novelty_score),
            Props.VIABILITY_RATING: prop_number(critique.viability_score),
            Props.CRITIQUE_SUMMARY: prop_text(critique.critique_summary),
            Props.STATUS: prop_select(new_status),
        })

    @staticmethod
    def _pair_rank_key(pair_run: _PairRun) -> tuple[bool, float, float, float]:
        critique = pair_run.final_critique
        return (
            BrainstormPipeline._meets_threshold(critique),
            critique.novelty_score + critique.viability_score,
            critique.contribution_score + critique.causal_rigor_score,
            -len(pair_run.rounds),
        )

    @staticmethod
    def _meets_threshold(critique: CritiqueResult) -> bool:
        return (
            critique.novelty_score >= config.thresholds.min_novelty_rating
            and critique.viability_score >= config.thresholds.min_viability_rating
        )

    # ------------------------------------------------------------------
    # Extra prompt builders
    # ------------------------------------------------------------------

    def _build_brainstorm_extra(self) -> str:
        """Format non-standard YAML sections into extra system prompt text."""
        instr = self.instructions
        parts: list[str] = []

        pf = (instr.get("proposal_format") or "").strip()
        if pf:
            parts.append(f"## Standard Proposal Format\n{pf}")

        ac = instr.get("angle_criteria", {})
        if ac:
            lines = ["## Proposal Quality Criteria"]
            for section, items in ac.items():
                lines.append(f"\n**{section.replace('_', ' ').title()}**")
                if isinstance(items, list):
                    lines.extend(f"- {item}" for item in items)
            parts.append("\n".join(lines))

        ri = (instr.get("refinement_instructions") or "").strip()
        if ri:
            parts.append(f"## Refinement Instructions\n{ri}")

        return "\n\n".join(parts)

    def _build_critic_extra(self) -> str:
        """Format non-standard YAML sections into extra system prompt text."""
        instr = self._critic_base.instructions
        parts: list[str] = []

        sr = instr.get("scoring_rubric", {})
        if sr:
            lines = ["## Scoring Rubric"]
            for dim, levels in sr.items():
                lines.append(f"\n**{dim.replace('_', ' ').title()}**")
                for score, desc in levels.items():
                    lines.append(f"- {score}: {desc}")
            parts.append("\n".join(lines))

        rl = (instr.get("recommendation_logic") or "").strip()
        if rl:
            parts.append(f"## Recommendation Logic\n{rl}")

        ri = (instr.get("refinement_instructions") or "").strip()
        if ri:
            parts.append(f"## Refinement Instructions\n{ri}")

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers — pure functions
# ---------------------------------------------------------------------------

def _format_round_page(
    round_num: int,
    brainstorm: BrainstormResult,
    critique: CritiqueResult,
    brainstorm_label: str,
    brainstorm_model: str,
    critic_label: str,
    critic_model: str,
) -> str:
    """Format a combined brainstorm + critique page for a single round."""
    return (
        f"## Run Metadata\n\n"
        f"- Round: {round_num}\n"
        f"- Brainstorm model: {brainstorm_label} ({brainstorm_model})\n"
        f"- Critic model: {critic_label} ({critic_model})\n\n"
        f"---\n\n"
        f"## Proposal\n\n"
        f"{brainstorm.full_report}\n\n"
        f"---\n\n"
        f"## Critique\n\n"
        f"{critique.full_report}"
    )


def _get_title(props: dict) -> str:
    rich = props.get(Props.NAME, {}).get("title", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _get_text(props: dict, key: str) -> str:
    rich = props.get(key, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _get_url(props: dict, key: str) -> str | None:
    return props.get(key, {}).get("url")


def _download_and_extract(url: str, max_pages: int = 50) -> str | None:
    """Download a PDF from a URL and extract text. Returns None on failure."""
    # Check if it's a local path that exists
    path = Path(url)
    if path.exists() and path.suffix.lower() == ".pdf":
        return _extract_pdf_text(path, max_pages) or None
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*",
            },
        ) as http:
            resp = http.get(url)
            resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = Path(tmp.name)
        text = _extract_pdf_text(tmp_path, max_pages)
        tmp_path.unlink(missing_ok=True)
        return text or None
    except Exception:
        return None


def _extract_pdf_text(path: Path, max_pages: int = 50) -> str:
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:max_pages]
        return "\n".join(page.extract_text() or "" for page in pages)
    except Exception:
        return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token for English text."""
    return len(text) // 4


import re as _re

_SECTION_RE = _re.compile(r"\n## ")


def _find_next_section(text: str) -> int:
    """Find the character index of the next '## ' header in text. Returns -1 if none."""
    m = _SECTION_RE.search(text)
    return m.start() if m else -1
