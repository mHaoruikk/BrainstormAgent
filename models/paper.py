"""
Pydantic data models shared across all agents.

These models define the structured output each agent produces and the
data that flows between pipeline stages. All fields that map to Notion
properties use the same names as notion/schema.py Props constants.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Core paper record
# ---------------------------------------------------------------------------

class PaperMetadata(BaseModel):
    """Populated by the Ingestion Agent; stored in the Notion database."""
    paper_id: str = Field(description="ArXiv ID (e.g. '2310.01234') or UUID for local PDFs")
    title: str
    authors: list[str]
    published_date: date | None = None
    abstract: str
    arxiv_url: str | None = None
    pdf_url: str | None = None
    # Set after the Notion page is created
    notion_page_id: str = ""


# ---------------------------------------------------------------------------
# Filter Agent output
# ---------------------------------------------------------------------------

class FilterResult(BaseModel):
    """Structured output from the Initial Filter Agent."""
    decision: Literal["pass", "reject", "uncertain"]
    reasoning_summary: str = Field(
        description="One paragraph summary written to the database column"
    )
    full_report: str = Field(
        description="Full Markdown report written as a Notion child page"
    )
    engineering_complexity: Literal["Low", "Medium", "High"]
    causal_relevance: Literal["High", "Medium", "Low", "None"]
    # Only populated when decision == "uncertain"
    question_for_researcher: str = ""


# ---------------------------------------------------------------------------
# Brainstorm Agent output
# ---------------------------------------------------------------------------

class BrainstormResult(BaseModel):
    """Structured output from one round of the Brainstorm Agent."""
    paper_summary: str = Field(description="3-sentence summary of the paper's core mechanism, written for a causal-inference audience")
    response_to_critique: str = Field(
        default="",
        description="For rounds >1 only: brief summary of what changed in response to the prior critique; leave empty on round 1",
    )
    title: str = Field(description="Section 1 — concise, specific research direction title")
    description: str = Field(description="Section 2 — Problem Statement: the concrete gap or limitation, formally stated")
    novelty_rationale: str = Field(description="Section 3 — Motivation & Hypothesis: why important, why novel, the central causal hypothesis and key lever")
    solution_sketch: str = Field(description="Section 4 — Proposed Method: formal setting, estimator or algorithm, key technical idea (use math notation)")
    experiment_plan: str = Field(description="Section 5 — Experiment Plan: datasets, baselines, primary metrics, potential benchmarks")
    open_questions: list[str] = Field(default_factory=list, description="2–3 open questions remaining after the proposal sketch")


# ---------------------------------------------------------------------------
# Critic Agent output
# ---------------------------------------------------------------------------

class CritiqueResult(BaseModel):
    """Structured output from one round of the Critic Agent."""
    novelty_score: float = Field(ge=1.0, le=5.0, description="How original is the proposed direction?")
    viability_score: float = Field(ge=1.0, le=5.0, description="How feasible for a 1–2 person team with limited compute?")
    contribution_score: float = Field(ge=1.0, le=5.0, description="How significant would the contribution be if successful?")
    causal_rigor_score: float = Field(ge=1.0, le=5.0, description="How sound is the causal/statistical reasoning?")
    strengths: list[str] = Field(description="Key strengths of the proposal")
    weaknesses: list[str] = Field(description="Key weaknesses or gaps to address")
    recommendation: Literal["pursue", "refine", "drop"] = Field(
        description="pursue = ready to develop; refine = promising but needs revision; drop = fundamentally flawed"
    )
    critique_summary: str = Field(description="One paragraph summary for the database column")
    full_report: str = Field(description="Full Markdown critique written to a Notion child page")


# ---------------------------------------------------------------------------
# Proposal Writer output
# ---------------------------------------------------------------------------

class ProposalResult(BaseModel):
    """Structured output from the Proposal Writer Agent."""
    title: str
    full_proposal: str = Field(description="Full structured proposal in Markdown")
