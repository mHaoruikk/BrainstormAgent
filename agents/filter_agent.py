"""
Initial Filter Agent.

Reads papers with Status=Unprocessed from the Notion database, calls an LLM
to judge their causal relevance and engineering complexity, and writes the
decision (Pass / Reject / Uncertain) back to Notion.

Decision modes:
  Mode 1 — Abstract only (default first pass)
  Mode 2 — Full text on demand when abstract is inconclusive and
            config.filter.use_full_text = true
  Mode 3 — Abstract only, always (config.filter.use_full_text = false)

Outputs per paper:
  - Updates DB columns: Pass Initial Filter, Filter Reasoning,
    Engineering Complexity, Causal Relevance, Status
  - Creates child page: Filter Report (Markdown)
  - Appends timestamped entry to the Agent Logs page
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

import httpx
from pypdf import PdfReader
from pydantic import BaseModel

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModelSettings

from agents.base import BaseAgent
from config import config
from models.paper import FilterResult
from notion.client import NotionClient
from notion.schema import (
    ChildPages,
    FilterDecision,
    Props,
    Status,
    prop_select,
    prop_text,
)

_SUMMARY_SYSTEM_PROMPT = """\
You are an expert in mathematical statistics, machine learning theory, and causal inference.
Produce a self-contained, mathematically precise summary of the academic paper for a causal
inference research group. Return your response as a well-structured Markdown document with
clear section headers. Important: inline math uses the format $...$, and block math uses the format $$...$$.\
"""


# ---------------------------------------------------------------------------
# Per-paper run outcome (returned to CLI)
# ---------------------------------------------------------------------------

class FilterRunResult(BaseModel):
    """Outcome of filtering a single paper."""
    paper_id: str
    title: str
    decision: str
    status: Literal["ok", "error", "skipped"]
    message: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class InitialFilterAgent(BaseAgent):

    def __init__(self, notion: NotionClient) -> None:
        super().__init__(instruction_file="filter_agent.yaml")
        self.notion = notion
        self._use_full_text: bool = config.filter.use_full_text
        self._agent = self.make_agent(
            model=config.models.filter,
            output_type=FilterResult,
            extra_prompt=self._build_rubric_prompt(),
        )
        # Separate agent for math summarization — no rubric, no structured output,
        # uses a cheaper reasoning model (o4-mini default = medium/standard thinking).
        self._summary_agent: Agent[None, str] = Agent(
            model=config.models.summary,
            output_type=str,
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            model_settings=OpenAIModelSettings(reasoning_effort="medium"),
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, paper_id: str | None = None) -> list[FilterRunResult]:
        """
        Filter papers with Status=Unprocessed.
        If paper_id is given, process only that paper.
        Returns one FilterRunResult per paper.
        """
        if paper_id:
            page = self.notion.get_paper_by_paper_id(paper_id)
            if page is None:
                return [FilterRunResult(
                    paper_id=paper_id, title="", decision="",
                    status="error", message=f"Paper not found: {paper_id}",
                )]
            current_status = page["properties"].get(Props.STATUS, {}).get("select", {})
            current_status = (current_status or {}).get("name", "")
            if current_status != Status.UNPROCESSED:
                title = _get_title(page["properties"])
                return [FilterRunResult(
                    paper_id=paper_id, title=title, decision="",
                    status="skipped",
                    message=f"Skipping '{title}': already filtered (status: {current_status})",
                )]
            pages = [page]
        else:
            pages = self.notion.query_papers(status=Status.UNPROCESSED)

        results: list[FilterRunResult] = []
        for page in pages:
            result = self._filter_paper(page)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Core filter logic for a single paper
    # ------------------------------------------------------------------

    def _filter_paper(self, page: dict) -> FilterRunResult:
        props = page["properties"]
        page_id = page["id"]
        title = _get_title(props)
        abstract = _get_text(props, Props.ABSTRACT)
        authors = _get_text(props, Props.AUTHORS)
        published = _get_date(props, Props.PUBLISHED_DATE)
        arxiv_url = _get_url(props, Props.ARXIV_URL)
        pdf_url = _get_url(props, Props.PDF_URL)
        paper_id = _get_text(props, Props.PAPER_ID)

        try:
            # --- Mode 1: abstract-only first pass ---
            user_msg = _build_user_message(title, authors, published, abstract)
            result: FilterResult = self._agent.run_sync(user_msg).output

            # --- Mode 2: fetch full text for uncertain cases ---
            if result.decision == "uncertain" and self._use_full_text:
                filter_text = self._fetch_full_text(page_id, pdf_url or arxiv_url, max_pages=5)
                if filter_text:
                    user_msg_full = _build_user_message(
                        title, authors, published, abstract, full_text=filter_text
                    )
                    result = self._agent.run_sync(user_msg_full).output

            self._write_to_notion(page_id, title, result)

            # --- Math summary for papers worth reading ---
            if result.decision in ("pass", "uncertain"):
                self._summarize_and_store(page_id, title, pdf_url or arxiv_url)

            log_msg = _format_log_message(title, result)
            return FilterRunResult(
                paper_id=paper_id,
                title=title,
                decision=result.decision,
                status="ok",
                message=log_msg,
            )

        except Exception as exc:
            return FilterRunResult(
                paper_id=paper_id,
                title=title,
                decision="",
                status="error",
                message=f"Error filtering '{title}': {exc}",
            )

    # ------------------------------------------------------------------
    # Notion write
    # ------------------------------------------------------------------

    def _write_to_notion(self, page_id: str, title: str, result: FilterResult) -> None:
        pass_value = {
            "pass": FilterDecision.YES,
            "reject": FilterDecision.NO,
            "uncertain": FilterDecision.UNCERTAIN,
        }[result.decision]

        new_status = {
            "pass": Status.FILTER_PASS,
            "reject": Status.FILTER_REJECT,
            "uncertain": Status.NEEDS_REVIEW,
        }[result.decision]

        self.notion.update_paper(page_id, {
            Props.PASS_INITIAL_FILTER: prop_select(pass_value),
            Props.FILTER_REASONING: prop_text(result.reasoning_summary),
            Props.ENGINEERING_COMPLEXITY: prop_select(result.engineering_complexity),
            Props.CAUSAL_RELEVANCE: prop_select(result.causal_relevance),
            Props.STATUS: prop_select(new_status),
        })

        # Guarantee the question section is present for uncertain decisions.
        # The LLM is instructed to include it in full_report, but if it omits
        # the section, we append it from the dedicated question_for_researcher
        # field so the Notion page always has a readable question.
        report = result.full_report
        if (
            result.decision == "uncertain"
            and result.question_for_researcher
            and "## Question for Researcher" not in report
        ):
            report += f"\n\n## Question for Researcher\n{result.question_for_researcher}"

        self.notion.create_child_page(
            parent_page_id=page_id,
            title=ChildPages.FILTER_REPORT,
            markdown=report,
        )

        # Log write is non-critical: a failure here must not mask the filter
        # result, which is already committed to Notion above.
        try:
            self.notion.append_to_log(_format_log_message(title, result))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # System prompt — rubric sections not handled by BaseAgent
    # ------------------------------------------------------------------

    def _build_rubric_prompt(self) -> str:
        """
        Convert the richer filter_agent.yaml sections (causal_relevance,
        red_flags, engineering_complexity, decision_logic) into prompt text.
        BaseAgent already handles role, output_instructions, and examples.
        """
        instr = self.instructions
        parts: list[str] = []

        # Causal relevance rubric
        cr = instr.get("causal_relevance", {})
        if cr:
            lines = ["## Causal Relevance Scoring Rubric"]
            for level_key in ("high", "medium", "low", "none"):
                lvl = cr.get(level_key, {})
                if not lvl:
                    continue
                label = lvl.get("label", level_key.title())
                desc = (lvl.get("description") or "").strip()
                lines.append(f"\n### {label}")
                if desc:
                    lines.append(desc)
                for sig in lvl.get("signals", []):
                    if isinstance(sig, dict):
                        lines.append(
                            f"- **{sig.get('name', '')}**: {(sig.get('detail') or '').strip()}"
                        )
                    else:
                        lines.append(f"- {sig}")
            parts.append("\n".join(lines))

        # Red flags
        rf = instr.get("red_flags", {})
        if rf:
            lines = [
                "## Red Flags (negative signals)",
                "Two or more red flags without a compensating causal signal → Reject.",
            ]
            for category, items in rf.items():
                lines.append(f"\n**{category.replace('_', ' ').title()}**")
                for item in items:
                    lines.append(f"- {item}")
            parts.append("\n".join(lines))

        # Engineering complexity rubric
        ec = instr.get("engineering_complexity", {})
        if ec:
            lines = ["## Engineering Complexity Scoring Rubric"]
            for level_key in ("low", "medium", "high"):
                lvl = ec.get(level_key, {})
                if not lvl:
                    continue
                label = lvl.get("label", level_key.title())
                threshold = lvl.get("threshold", "")
                lines.append(f"\n### {label}")
                if threshold:
                    lines.append(f"Threshold: {threshold}")
                for sig in lvl.get("signals", []):
                    lines.append(f"- {sig}")
            parts.append("\n".join(lines))

        # Decision logic
        dl = (instr.get("decision_logic") or "").strip()
        if dl:
            parts.append(f"## Decision Logic\n{dl}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Full-text PDF fetching (Mode 2)
    # ------------------------------------------------------------------

    def _get_pdf_url(self, page_id: str, fallback_url: str | None) -> str | None:
        """
        Resolve the best PDF URL for a paper.
        Checks the Notion page attachment first (always up-to-date),
        then falls back to the property URL.
        """
        url = self.notion.get_pdf_url_from_page(page_id)
        return url or fallback_url

    def _fetch_full_text(self, page_id: str, fallback_url: str | None, max_pages: int = 5) -> str | None:
        url = self._get_pdf_url(page_id, fallback_url)
        if not url:
            return None
        path = Path(url)
        if path.exists() and path.suffix.lower() == ".pdf":
            return _extract_pdf_text(path, max_pages=max_pages) or None
        tmp = _download_pdf(url)
        if tmp is None:
            return None
        text = _extract_pdf_text(tmp, max_pages=max_pages)
        tmp.unlink(missing_ok=True)
        return text or None

    def _summarize_and_store(self, page_id: str, title: str, fallback_url: str | None) -> None:
        """Fetch the full paper, produce a math-level summary, store as a child page."""
        full_text = self._fetch_full_text(page_id, fallback_url, max_pages=50)
        if not full_text:
            return
        user_msg = _build_summary_message(title, full_text)
        try:
            summary: str = self._summary_agent.run_sync(user_msg).output
            self.notion.create_child_page(
                parent_page_id=page_id,
                title=ChildPages.MATH_SUMMARY,
                markdown=summary,
            )
        except Exception:
            # Summary is non-critical; filter result is already committed.
            pass


# ---------------------------------------------------------------------------
# Helpers — pure functions
# ---------------------------------------------------------------------------

def _build_user_message(
    title: str,
    authors: str,
    published: str,
    abstract: str,
    full_text: str | None = None,
) -> str:
    lines = [
        f"**Title:** {title}",
        f"**Authors:** {authors or 'Unknown'}",
        f"**Published:** {published or 'Unknown'}",
        "",
        f"**Abstract:**\n{abstract}",
    ]
    if full_text:
        lines += [
            "",
            "**Full Text (introduction and conclusion, first ~5 pages):**",
            full_text[:6000],
        ]
    lines += [
        "",
        "Please assess this paper and return a FilterResult.",
    ]
    return "\n".join(lines)


def _build_summary_message(title: str, full_text: str) -> str:
    return (
        f"**Paper:** {title}\n\n"
        "Summarize this paper by extracting the most essential aspects:\n\n"
        "1. **Problem** — What problem is the paper trying to solve?\n"
        "2. **Setting** — What is the formal problem setting (domain, data generating process, key assumptions)?\n"
        "3. **Proposed Solution & Key Innovations** — What method does the paper propose? "
        "What are the key ideas and contributions?\n\n"
        "Use clear mathematics (formal definitions, notation, equations) instead of plain text wherever possible "
        "to explain the methods and essential logic. Where possible, frame the core problem as a statistical "
        "problem written out formally — you do not need to provide a solution, just the formulation.\n\n"
        "---\n\n"
        f"{full_text}"
    )


def _format_log_message(title: str, result: FilterResult) -> str:
    if result.decision == "uncertain":
        return f"Filter: '{title}' → Needs Review — question written to Filter Report"
    return (
        f"Filter: '{title}' → {result.decision.capitalize()} "
        f"(Causal Relevance: {result.causal_relevance}, "
        f"Complexity: {result.engineering_complexity})"
    )


def _download_pdf(url: str) -> Path | None:
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
            return Path(tmp.name)
    except Exception:
        return None


def _extract_pdf_text(path: Path, max_pages: int = 5) -> str:
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:max_pages]
        return "\n".join(page.extract_text() or "" for page in pages)
    except Exception:
        return ""


def _get_title(props: dict) -> str:
    rich = props.get(Props.NAME, {}).get("title", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _get_text(props: dict, key: str) -> str:
    rich = props.get(key, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _get_date(props: dict, key: str) -> str:
    date_prop = props.get(key, {}).get("date")
    return date_prop["start"] if date_prop else ""


def _get_url(props: dict, key: str) -> str | None:
    return props.get(key, {}).get("url")
