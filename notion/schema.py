"""
Notion schema constants and property/block builders.

All Notion property names and status values are defined here as class
attributes so a typo becomes an AttributeError instead of a silent wrong
API call. Property builders handle Notion's verbose JSON format.
"""

from __future__ import annotations

from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class Props:
    """Notion database property names — must match your database exactly."""
    NAME = "Name"
    PAPER_ID = "Paper ID"
    AUTHORS = "Authors"
    PUBLISHED_DATE = "Published Date"
    PROCESSED_DATE = "Processed Date"
    ABSTRACT = "Abstract"
    ARXIV_URL = "ArXiv URL"
    PDF_URL = "PDF URL"
    STATUS = "Status"
    PASS_INITIAL_FILTER = "Pass Initial Filter"
    FILTER_REASONING = "Filter Reasoning"
    ENGINEERING_COMPLEXITY = "Engineering Complexity"
    CAUSAL_RELEVANCE = "Causal Relevance"
    NOVELTY_RATING = "Novelty Rating"
    VIABILITY_RATING = "Viability Rating"
    CRITIQUE_SUMMARY = "Critique Summary"
    TAGS = "Tags"
    NOTES = "Notes"


class Status:
    UNPROCESSED = "Unprocessed"
    FILTER_PASS = "Filter:Pass"
    FILTER_REJECT = "Filter:Reject"
    NEEDS_REVIEW = "Needs Review"
    BRAINSTORMING = "Brainstorming"
    CRITIQUED = "Critiqued"
    PROPOSAL_DRAFTED = "Proposal:Drafted"
    PROPOSAL_REJECTED = "Proposal:Rejected"
    ARCHIVED = "Archived"

    ALL = [
        UNPROCESSED, FILTER_PASS, FILTER_REJECT, NEEDS_REVIEW,
        BRAINSTORMING, CRITIQUED, PROPOSAL_DRAFTED, PROPOSAL_REJECTED, ARCHIVED,
    ]


class FilterDecision:
    YES = "Yes"
    NO = "No"
    UNCERTAIN = "Uncertain"


class Complexity:
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class CausalRelevance:
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    NONE = "None"


class ChildPages:
    """Titles of agent-generated child pages under each paper page."""
    FILTER_REPORT = "Filter Report"
    MATH_SUMMARY = "Math Summary"
    CRITIQUE_REPORT = "Critique Report"
    FINAL_PROPOSAL = "Final Proposal"

    @staticmethod
    def brainstorm(label: str) -> str:
        return f"Brainstorm — {label}"

    @staticmethod
    def brainstorm_round(
        n: int,
        brainstorm_label: str | None = None,
        critic_label: str | None = None,
    ) -> str:
        labels = [label for label in (brainstorm_label, critic_label) if label]
        if labels:
            return f"Brainstorm — {' + '.join(labels)} — Round {n}"
        return f"Brainstorm — Round {n}"


# ---------------------------------------------------------------------------
# Property builders
# Notion's API requires a specific JSON shape for each property type.
# ---------------------------------------------------------------------------

def prop_title(value: str) -> dict:
    return {"title": [{"text": {"content": _truncate(value, 2000)}}]}


def prop_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": _truncate(value, 2000)}}]}


def prop_date(value: date | str | None) -> dict:
    if value is None:
        return {"date": None}
    start = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return {"date": {"start": start}}


def prop_url(value: str | None) -> dict:
    return {"url": value}


def prop_select(value: str) -> dict:
    return {"select": {"name": value}}


def prop_number(value: float) -> dict:
    return {"number": value}


def prop_multi_select(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in values]}


# ---------------------------------------------------------------------------
# Block builders
# Used when writing child page content (Markdown → Notion blocks).
# ---------------------------------------------------------------------------

def block_paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": _truncate(text, 2000)}}]},
    }


def block_heading(text: str, level: int) -> dict:
    assert level in (1, 2, 3)
    h = f"heading_{level}"
    return {
        "type": h,
        h: {"rich_text": [{"type": "text", "text": {"content": _truncate(text, 2000)}}]},
    }


def block_bullet(text: str) -> dict:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": _truncate(text, 2000)}}]},
    }


def block_numbered(text: str) -> dict:
    return {
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": _truncate(text, 2000)}}]},
    }


def block_divider() -> dict:
    return {"type": "divider", "divider": {}}


def block_pdf_external(url: str) -> dict:
    """Inline PDF viewer block for an external URL (arXiv, OpenReview, etc.)."""
    return {
        "type": "pdf",
        "pdf": {
            "type": "external",
            "external": {"url": url},
        },
    }


# ---------------------------------------------------------------------------
# Markdown → Notion blocks
# Handles: h1/h2/h3, bullets, numbered lists, dividers (---), paragraphs.
# Bold/italic markers are stripped (plain text only) for simplicity.
# ---------------------------------------------------------------------------

def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    for line in md.splitlines():
        stripped = line.rstrip()
        bare = stripped.lstrip()

        if not bare:
            continue
        if bare.startswith("### "):
            blocks.append(block_heading(_strip_inline(bare[4:]), 3))
        elif bare.startswith("## "):
            blocks.append(block_heading(_strip_inline(bare[3:]), 2))
        elif bare.startswith("# "):
            blocks.append(block_heading(_strip_inline(bare[2:]), 1))
        elif bare in ("---", "***", "___"):
            blocks.append(block_divider())
        elif bare.startswith(("- ", "* ", "+ ")):
            blocks.append(block_bullet(_strip_inline(bare[2:])))
        elif len(bare) > 2 and bare[0].isdigit() and bare[1] in ".)" and bare[2] == " ":
            blocks.append(block_numbered(_strip_inline(bare[3:])))
        else:
            blocks.append(block_paragraph(_strip_inline(stripped)))

    return blocks


def blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion block objects (for agent consumption)."""
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        inner = block.get(btype, {})
        rich = inner.get("rich_text", [])
        if rich:
            lines.append("".join(rt.get("plain_text", "") for rt in rich))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    return text[:limit] if len(text) > limit else text


def _strip_inline(text: str) -> str:
    """Remove bold/italic markdown markers for plain Notion text."""
    return text.replace("**", "").replace("__", "").replace("*", "").replace("_", " ").strip()
