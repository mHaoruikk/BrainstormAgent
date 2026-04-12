# BrainstormAgent — Multi-Agent Research Ideation Framework

## Overview

BrainstormAgent is a CLI-driven multi-agent pipeline that helps a researcher identify, filter, and develop novel research ideas from academic papers. The central theme is combining causal inference with LLMs/agents. The pipeline moves a paper through five stages — ingestion, filtering, brainstorming, critique, and proposal writing — with all state and documents stored in Notion.

**Design principles:**
- **Notion as the source of truth** — every intermediate artifact is written back to Notion so the researcher can inspect, override, or annotate at any stage.
- **Human in the loop** — uncertain filter decisions pause in Notion (status `Needs Review`) rather than blocking the CLI.
- **Swappable LLM backends** — each agent declares which model it uses; this is configurable in `config.yaml` and overridable per CLI invocation.
- **Thin, explicit abstractions** — built on PydanticAI and the raw Notion REST API. No heavy orchestration frameworks. Easy to read, easy to extend.

---

## Architecture

```
 User Input
 ┌────────────────────────────────────────────────────────┐
 │  Research Inclination (research_direction.yaml)        │
 │  Per-agent Instruction Files (instructions/*.yaml)     │
 └────────────────────────────────────────────────────────┘
              │  shared context loaded at startup
              ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        CLI  (cli.py)                                │
 │  add-paper │ run-filter │ run-brainstorm │ run-critique │ run-proposal│
 │                     run-pipeline (all stages)                       │
 └──────────┬───────────────┬───────────────┬───────────────┬──────────┘
            │               │               │               │
            ▼               ▼               ▼               ▼
   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
   │   Ingestion  │ │   Filter     │ │  Brainstorm  │ │   Critic     │
   │   Agent      │ │   Agent      │ │  Agents      │ │   Agent      │
   └──────┬───────┘ └──────┬───────┘ │  (GPT-4o +   │ └──────┬───────┘
          │                │         │   Claude)     │        │
          │                │         └──────┬───────┘        │
          │                │                │                 ▼
          │                │                │        ┌──────────────┐
          │                │                │        │   Proposal   │
          │                │                │        │   Writer     │
          │                │                │        └──────┬───────┘
          │                │                │               │
          └────────────────┴────────────────┴───────────────┘
                                   │
                                   ▼
                         ┌──────────────────┐
                         │  Notion Database  │
                         │  + Sub-pages      │
                         └──────────────────┘
```

---

## Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Standard for research tooling |
| Agent framework | PydanticAI | Lightweight, multi-backend, structured I/O |
| LLM backends | OpenAI (GPT-4o), Anthropic (Claude), Google (Gemini) | Configurable per agent |
| Storage / UI | Notion REST API | Single view for papers, docs, history |
| ArXiv fetching | `arxiv` Python library | Official, simple |
| PDF parsing | `pypdf` | Lightweight, no server needed |
| CLI | `typer` | Clean, type-safe CLI from function signatures |
| Config | `pydantic-settings` + `.env` + `config.yaml` | Layered, explicit |
| Instruction files | YAML | Human-readable, structured, commentable |

---

## Notion Structure

### The Papers Database

One central Notion database named **"Research Papers"** is the backbone of the system. Each row is simultaneously a structured record (properties = columns) and a Notion page (where all sub-documents live as child pages).

#### Database Properties (columns)

| Property | Type | Description |
|---|---|---|
| `Name` | Title | Paper title |
| `Paper ID` | Text | ArXiv ID or local UUID for PDF uploads |
| `Authors` | Text | Comma-separated author list |
| `Published Date` | Date | Paper publication date |
| `Processed Date` | Date | Date first ingested by the system |
| `Abstract` | Text | Paper abstract |
| `ArXiv URL` | URL | Link to arxiv.org page |
| `PDF URL` | URL | Direct PDF link or Notion file |
| `Status` | Select | Pipeline stage (see below) |
| `Pass Initial Filter` | Select | `Yes` / `No` / `Uncertain` |
| `Filter Reasoning` | Text | One-paragraph summary from filter agent |
| `Engineering Complexity` | Select | `Low` / `Medium` / `High` |
| `Causal Relevance` | Select | `High` / `Medium` / `Low` / `None` |
| `Novelty Rating` | Number | 1–5 score from critic agent |
| `Viability Rating` | Number | 1–5 score from critic agent |
| `Critique Summary` | Text | One-paragraph summary from critic agent |
| `Tags` | Multi-select | Topic tags (e.g., `RAG`, `RLHF`, `causality`) |
| `Notes` | Text | Free-form human annotations |

#### Status Values (pipeline stages)

```
Unprocessed → Filter:Pass → Filter:Reject → Needs Review
                   ↓
             Brainstorming
                   ↓
              Critiqued
                   ↓
          Proposal:Drafted  /  Proposal:Rejected
                   ↓
               Archived
```

#### Child Pages per Paper

Each paper page contains up to four child pages written by agents:

```
[Paper Page]
├── Filter Report          ← written by Filter Agent
├── Brainstorm — GPT-4o    ← written by Brainstorm Agent (OpenAI)
├── Brainstorm — Claude    ← written by Brainstorm Agent (Anthropic)
├── Critique Report        ← written by Critic Agent
└── Final Proposal         ← written by Proposal Writer Agent
```

Child pages not yet generated simply don't exist (no blank placeholders created in advance).

### Workspace Layout

```
Notion Workspace
├── Research Papers         ← Main database (described above)
├── Research Directions     ← Plain page: stores your research inclination description
│                              (human-editable, referenced by all agents at runtime)
└── Agent Logs              ← Plain page: append-only log of pipeline runs
```

The `Research Directions` page is the single source of truth for the research theme. Agents fetch it at startup and include it in their system prompt.

---

## Agents

### 1. Paper Ingestion Agent

**Purpose:** Add a paper to the Notion database and return a paper record.

**Inputs:**
- ArXiv URL (e.g., `https://arxiv.org/abs/2310.01234`) — fetches via `arxiv` library
- ArXiv paper name/search query — searches arxiv, picks the top match, confirms with user
- Local PDF file path — extracts title, authors, abstract using `pypdf` + LLM call

**Outputs:**
- Creates a new row in the Notion database
- Sets `Status = Unprocessed`
- Fills all available metadata columns

**LLM use:** Only for PDF metadata extraction (extracts structured metadata from first 2 pages). Configurable model.

**Duplicate detection:** Before creating a row, checks if `Paper ID` already exists in the database.

---

### 2. Initial Filter Agent

**Purpose:** Review unprocessed papers and make a binary judgment on research viability.

**Trigger:** Processes all papers with `Status = Unprocessed`.

**Decision logic (guided by `instructions/filter_agent.yaml`):**
- `Pass` — paper is relevant, causal angle plausible, engineering complexity acceptable
- `Reject` — clearly out of scope (e.g., pure pre-training, hardware-focused, no inference-time hook)
- `Uncertain` — needs human judgment; sets `Status = Needs Review` and adds a comment to the Notion page

**Human-in-the-loop:** For `Uncertain` papers, the agent writes a detailed question in the Filter Report child page, tags it `Needs Review`, and exits. The researcher reviews in Notion, manually sets status to `Filter:Pass` or `Filter:Reject`, and re-runs the pipeline.

**Outputs written to Notion:**
- `Pass Initial Filter` property
- `Filter Reasoning` property (one paragraph)
- `Engineering Complexity` property
- `Causal Relevance` property
- `Status` property
- New child page: `Filter Report` (full reasoning, evidence, decision rationale)

---

### 3. Brainstorm Agents

**Purpose:** Generate causal research angles and solution sketches for papers that passed filtering.

**Trigger:** Processes all papers with `Status = Filter:Pass`.

**Design:** Two agents run sequentially (extendable to parallel), each with a different LLM backbone:
- `BrainstormAgent(model="gpt-4o")` — writes `Brainstorm — GPT-4o` child page
- `BrainstormAgent(model="claude-opus-4-6")` — writes `Brainstorm — Claude` child page

Both share the same system prompt structure (from `instructions/brainstorm_agent.yaml`) but produce independent outputs. The diversity of backbones is intentional — different reasoning styles surface different angles.

**Each brainstorm output includes:**
1. Summary of the paper's core mechanism
2. Two to four causal research angles (each with: angle description, why it's novel, what data/method it requires)
3. Proposed solution sketch for each angle (methodology, expected contribution)
4. Open questions and risks

**Outputs written to Notion:**
- New child page: `Brainstorm — [Model]` per agent
- `Status = Brainstorming` while running, then `Critiqued` handoff (status set after both complete)

---

### 4. Critic Agent

**Purpose:** Evaluate the brainstorm proposals for novelty, contribution, and practical viability.

**Trigger:** Processes all papers with `Status = Brainstorming` (both brainstorm pages exist).

**Evaluation criteria (from `instructions/critic_agent.yaml`):**
- **Novelty** (1–5): Is this direction genuinely new? Has it been explored?
- **Contribution** (1–5): Would a paper on this be accepted at a top venue?
- **Viability** (1–5): Can a small team (1–2 researchers, no massive compute) execute this?
- **Causal Rigor** (1–5): Is the causal framing tight, not just superficial?

The critic reads both brainstorm documents and produces a unified critique. It may favor the best angle across both documents, or note contradictions.

**Outputs written to Notion:**
- `Novelty Rating` property (average or best-angle score)
- `Viability Rating` property
- `Critique Summary` property (one paragraph)
- New child page: `Critique Report` (full per-angle breakdown, ratings, recommendations)
- `Status = Critiqued`

---

### 5. Proposal Writer Agent

**Purpose:** Write a structured research proposal for proposals that meet the quality threshold.

**Trigger:** Papers with `Status = Critiqued` AND `Novelty Rating >= threshold` (default: `3`, configurable in `config.yaml`) AND `Viability Rating >= threshold`.

**Proposal structure:**
1. Title
2. Motivation and Problem Statement
3. Related Work (brief, from paper context)
4. Proposed Approach (causal framework, methodology)
5. Expected Contributions
6. Experimental Plan (datasets, baselines, metrics)
7. Risks and Mitigations
8. Timeline Estimate

**Outputs written to Notion:**
- New child page: `Final Proposal` (full structured document as Notion blocks)
- `Status = Proposal:Drafted`

**If below threshold:** Sets `Status = Proposal:Rejected`, skips writing.

---

## Shared Context

Every agent receives the following at startup:

```python
class ResearchContext(BaseModel):
    research_direction: str        # full text from research_direction.yaml
    current_date: str
    researcher_profile: str        # brief self-description (from config.yaml)
```

This is injected into every agent's system prompt, ensuring all agents are aligned on the research theme without repeating configuration.

---

## Data Models

All inter-agent data is typed via Pydantic models defined in `models/`.

```python
# models/paper.py
class PaperMetadata(BaseModel):
    paper_id: str
    title: str
    authors: list[str]
    published_date: date | None
    abstract: str
    arxiv_url: str | None
    pdf_url: str | None
    notion_page_id: str

class FilterResult(BaseModel):
    decision: Literal["pass", "reject", "uncertain"]
    reasoning: str                  # full report (Markdown)
    reasoning_summary: str          # one paragraph for DB column
    engineering_complexity: Literal["Low", "Medium", "High"]
    causal_relevance: Literal["High", "Medium", "Low", "None"]

class BrainstormResult(BaseModel):
    model_used: str
    angles: list[ResearchAngle]     # see below
    full_report: str                # Markdown, written as Notion child page

class ResearchAngle(BaseModel):
    title: str
    description: str
    novelty_rationale: str
    required_data_or_method: str
    solution_sketch: str
    open_questions: list[str]

class CritiqueResult(BaseModel):
    novelty_rating: float           # 1.0–5.0
    viability_rating: float
    contribution_rating: float
    causal_rigor_rating: float
    summary: str                    # one paragraph
    full_report: str                # Markdown

class ProposalResult(BaseModel):
    title: str
    full_proposal: str              # Markdown
```

---

## Instruction Files

Instruction files live in `instructions/` and are YAML. Every agent loads its own file plus `research_direction.yaml`.

### `instructions/research_direction.yaml` — Research Inclination

```yaml
title: "Combining Causal Inference with LLMs and Agents"

description: |
  We are a small research group focused on causal inference. Our goal is to find
  research opportunities at the intersection of causality and large language models
  or AI agents. We are NOT interested in scaling pre-training. We ARE interested in
  using causal tools to improve LLM reasoning, robustness, alignment, or to use
  LLMs as tools inside causal pipelines.

researcher_profile: |
  Small team (1–2 researchers). Strong background in causal inference (SCMs,
  do-calculus, instrumental variables, difference-in-differences). Moderate
  ML engineering capacity — no large-scale GPU clusters. Prefer theory + small
  experiment setups over large benchmarks.

non_goals:
  - Pre-training or fine-tuning LLMs from scratch
  - Pure NLP tasks without causal framing
  - Hardware or systems research
  - Papers requiring >8 A100s to reproduce

goals:
  - Causal reasoning in LLM inference pipelines
  - Using causal methods to diagnose or improve LLM behavior
  - Causal structure in agent decision-making
  - Identifiability and confounding in LLM-generated data
```

### `instructions/filter_agent.yaml` — Filter Agent Instructions

```yaml
name: Initial Filter Agent
role: |
  You are a critical research advisor for a causal inference research group.
  Your job is to quickly assess whether a paper is worth deeper investigation.

criteria:
  must_reject:
    - "Paper is primarily about pre-training or architecture scaling"
    - "No inference-time or post-training hook for causal methods"
    - "Purely hardware, systems, or infrastructure focused"
  likely_pass:
    - "Paper introduces a reasoning benchmark that causal methods could improve"
    - "Paper studies LLM behavior in ways that suggest confounding or spurious correlations"
    - "Paper uses RL, feedback, or decision-making (causal structure present)"
  uncertainty_triggers:
    - "Paper is adjacent but the causal angle is not immediately obvious"
    - "Paper requires significant engineering but the idea is compelling"

output_instructions: |
  Write a Filter Report in Markdown with sections:
  1. Paper Summary (3 sentences)
  2. Decision: Pass / Reject / Uncertain
  3. Reasoning (bullet points)
  4. If Uncertain: specific question for the researcher to resolve

examples:
  - title: "GPT-4 Technical Report"
    decision: reject
    reason: "Pure pre-training and scaling. No inference-time causal angle."
  - title: "Chain-of-Thought Prompting Elicits Reasoning in LLMs"
    decision: uncertain
    question: "CoT could be analyzed causally (does structured reasoning reduce confounding?). Is this direction interesting to you?"
  - title: "Causal Abstraction Aligns with Human Explanations"
    decision: pass
    reason: "Directly uses causal abstraction to interpret model behavior."
```

### `instructions/brainstorm_agent.yaml` and `instructions/critic_agent.yaml`

Follow the same YAML structure: `name`, `role`, `criteria`, `output_instructions`, `examples`.

---

## Project Structure

```
BrainstormAgent/
├── docs/
│   ├── main.md                     ← this file
│   └── notion_setup.md             ← Notion workspace setup guide
│
├── instructions/
│   ├── research_direction.yaml     ← shared research context
│   ├── filter_agent.yaml
│   ├── brainstorm_agent.yaml
│   └── critic_agent.yaml
│
├── agents/
│   ├── __init__.py
│   ├── base.py                     ← BaseAgent: loads context, wraps PydanticAI Agent
│   ├── ingestion.py                ← PaperIngestionAgent
│   ├── filter_agent.py             ← InitialFilterAgent
│   ├── brainstorm_agent.py         ← BrainstormAgent (model-agnostic)
│   ├── critic_agent.py             ← CriticAgent
│   └── proposal_writer.py          ← ProposalWriterAgent
│
├── notion/
│   ├── __init__.py
│   ├── client.py                   ← thin Notion REST API wrapper
│   └── schema.py                   ← property name constants, page builders
│
├── models/
│   ├── __init__.py
│   └── paper.py                    ← all Pydantic data models
│
├── config.py                       ← loads config.yaml + .env via pydantic-settings
├── config.yaml                     ← non-secret config (model choices, thresholds)
├── cli.py                          ← typer CLI entry point
│
├── .env.example                    ← template for secrets
├── requirements.txt
└── README.md
```

---

## CLI Reference

```bash
# Add a paper by ArXiv URL
python cli.py add-paper --url "https://arxiv.org/abs/2310.01234"

# Add a paper by search query (interactive confirmation)
python cli.py add-paper --query "causal abstraction transformer"

# Add a paper from a local PDF
python cli.py add-paper --pdf "/path/to/paper.pdf"

# Run the full pipeline on all eligible papers
python cli.py run-pipeline

# Run individual stages (process only papers in the correct status)
python cli.py run-filter
python cli.py run-brainstorm
python cli.py run-critique
python cli.py run-proposal

# Run a single stage on a specific paper (by Notion page ID or ArXiv ID)
python cli.py run-filter   --paper-id "2310.01234"
python cli.py run-brainstorm --paper-id "2310.01234" --model "gpt-4o"

# List papers by status
python cli.py list --status "Needs Review"
python cli.py list --status "Critiqued"

# Show pipeline status summary
python cli.py status
```

---

## Configuration

### `config.yaml`

```yaml
# LLM model assignments per agent
models:
  ingestion: "gpt-4o-mini"          # cheap, only for PDF metadata extraction
  filter: "claude-opus-4-6"
  brainstorm:
    - "gpt-4o"
    - "claude-opus-4-6"
  critic: "gpt-4o"
  proposal_writer: "claude-opus-4-6"

# Quality thresholds for proposal writing
thresholds:
  min_novelty_rating: 3.0
  min_viability_rating: 3.0

# Researcher profile
researcher:
  name: "Research Team"
  profile_summary: "Causal inference group, 1-2 researchers, moderate ML engineering capacity"

# Notion
notion:
  database_name: "Research Papers"  # used for display only; ID comes from .env
```

### `.env` (from `.env.example`)

```bash
# Notion
NOTION_API_TOKEN=YOUR_NOTION_INTEGRATION_TOKEN
NOTION_DATABASE_ID=YOUR_NOTION_DATABASE_ID

# LLM providers (add only the ones you use)
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
ANTHROPIC_API_KEY=YOUR_ANTHROPIC_API_KEY
GOOGLE_API_KEY=YOUR_GOOGLE_API_KEY
```

---

## Extension Points

The framework is deliberately thin so each of these is a small addition, not a rewrite:

| Future feature | Where to extend |
|---|---|
| Add a new LLM backend | Add model string to `config.yaml`; PydanticAI handles the rest |
| Add a new agent (e.g., related-work finder) | Subclass `BaseAgent` in `agents/`, add CLI command in `cli.py` |
| Multi-round proposal refinement | Loop `run-brainstorm` + `run-critique` N times; add round counter to DB |
| Automatic ArXiv monitoring | Cron job calling `add-paper` with a saved search query |
| Slack/email notifications for `Needs Review` | Add a notifier hook in `filter_agent.py` after writing Uncertain status |
| Vector search over past papers | Embed abstracts on ingestion, store in a sidecar DB (e.g., Chroma) |
| Web UI | The Notion database IS the UI for now; later wrap CLI in FastAPI |

---

## Pipeline Walkthrough (End-to-End Example)

```
1. Researcher runs:
   python cli.py add-paper --url "https://arxiv.org/abs/2402.01817"

   → Ingestion Agent fetches metadata from ArXiv
   → Creates Notion row: Status=Unprocessed, fills all metadata columns

2. Researcher runs:
   python cli.py run-filter

   → Filter Agent reads all Unprocessed papers
   → Calls LLM with paper abstract + filter_agent.yaml instructions + research_direction.yaml
   → Decision: Pass
   → Writes Filter Report child page
   → Updates DB: Status=Filter:Pass, Pass Initial Filter=Yes, etc.

3. Researcher runs:
   python cli.py run-brainstorm

   → BrainstormAgent(gpt-4o) runs, writes "Brainstorm — GPT-4o" child page
   → BrainstormAgent(claude-opus-4-6) runs, writes "Brainstorm — Claude" child page
   → Updates DB: Status=Brainstorming → (after both complete) leaves for critic

4. Researcher runs:
   python cli.py run-critique

   → Critic Agent reads both brainstorm pages
   → Produces Critique Report child page
   → Updates DB: Novelty Rating=4.0, Viability Rating=3.5, Status=Critiqued

5. Researcher runs:
   python cli.py run-proposal

   → Novelty=4.0 >= 3.0, Viability=3.5 >= 3.0 → threshold met
   → Proposal Writer drafts structured proposal
   → Writes Final Proposal child page in Notion
   → Updates DB: Status=Proposal:Drafted
```
