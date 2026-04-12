# Filter Agent

The Filter Agent is the second stage of the pipeline. It reads unprocessed paper records from the Notion database, judges whether each paper is worth deeper investigation from the perspective of a causal inference researcher, and writes its decision back to Notion. Papers that pass move forward to brainstorming; papers that clearly fail are archived with a written explanation; papers where the agent is unsure are flagged for human review.

---

## Role in the Pipeline

```
Ingestion Agent
      ↓
  Status = Unprocessed
      ↓
 Filter Agent              ← this agent
      ↓
  Status = Filter:Pass     → Brainstorm Agent
  Status = Filter:Reject   → archived (end of pipeline)
  Status = Needs Review    → researcher reviews manually in Notion, then re-runs
```

---

## What It Does

1. Queries the Notion database for all papers with `Status = Unprocessed`
2. For each paper, builds a context packet (see Inputs below)
3. Calls an LLM with the research direction, filter instructions, and paper context
4. Parses the structured response into a `FilterResult`
5. Writes the decision back to Notion:
   - Updates database columns (`Pass Initial Filter`, `Filter Reasoning`, `Engineering Complexity`, `Causal Relevance`, `Status`)
   - Creates a `Filter Report` child page with the full reasoning
6. Moves on to the next unprocessed paper

The agent processes papers sequentially by default. It does not skip a paper even if a previous one errored — each paper is handled independently.

---

## Inputs

### From the Notion database (per paper)

The agent reads the following from the already-populated paper record:

| Field | Used for |
|---|---|
| `Title` | Identification and LLM context |
| `Abstract` | Primary signal for the filter decision |
| `Authors` | Optional context (known causal researchers, etc.) |
| `Published Date` | Recency check |
| `ArXiv URL` | Used to fetch full text when needed (see below) |
| `PDF URL` | Fallback for full-text fetch |

### From instruction files (loaded once at startup)

- `instructions/research_direction.yaml` — the shared research theme, goals, and non-goals; injected into every agent's system prompt
- `instructions/filter_agent.yaml` — filter-specific criteria: what to pass, what to reject, what to flag as uncertain, and how to write the report

### How the agent accesses paper content

This is a key design decision. The filter agent has three modes for reading paper content, in increasing depth:

**Mode 1 — Abstract only (default)**
The abstract is already stored in the Notion database from the ingestion step. The agent reads it directly from the DB without any network call. This is sufficient for most filter decisions: a pre-training paper is obvious from the abstract; a paper about causal reasoning in agents is obviously relevant from the abstract.

**Mode 2 — Full text on demand (for Uncertain cases)**
When the agent's initial read of the abstract is inconclusive, it can fetch the full paper text. It does this by:
1. Reading the `PDF URL` column from the Notion record
2. If the URL is a web URL (arXiv, OpenReview): downloading and parsing the PDF, extracting the introduction and conclusion sections (first ~5 pages)
3. If the URL is a local path: reading the file from disk
4. Re-running the LLM call with the richer context before making a final decision

**Mode 3 — Abstract only, always (strict)**
A configuration flag `filter.use_full_text: false` in `config.yaml` disables Mode 2 entirely. In this mode, all uncertain cases are immediately escalated to `Needs Review` without fetching the PDF. Faster, cheaper, more human oversight.

**Recommended default:** Mode 2 — use the abstract for a first pass; only fetch the full text if the abstract is genuinely ambiguous. This minimises LLM cost while avoiding unnecessary human escalations for papers that a few extra paragraphs would resolve.

---

## Outputs

### Notion database columns updated per paper

| Column | Value written |
|---|---|
| `Pass Initial Filter` | `Yes` / `No` / `Uncertain` |
| `Filter Reasoning` | One-paragraph plain-text summary of the decision |
| `Engineering Complexity` | `Low` / `Medium` / `High` |
| `Causal Relevance` | `High` / `Medium` / `Low` / `None` |
| `Status` | `Filter:Pass` / `Filter:Reject` / `Needs Review` |

### Notion child page created per paper

A child page titled **`Filter Report`** is created under the paper's Notion page. It is a structured Markdown document with the following sections:

```
# Filter Report

## Paper Summary
(3 sentences: what problem the paper addresses, the method or contribution,
the experimental setting or scale)

## Causal Relevance: [High / Medium / Low / None]
(2–4 bullet points citing specific signals from the abstract; red flags listed
explicitly if they apply)

## Engineering Complexity: [Low / Medium / High]
(1–2 bullet points referencing model size, dataset scale, or compute details
from the abstract)

## Decision: [Pass / Reject / Uncertain]
(One sentence stating the decision and the primary reason)

## Question for Researcher  [only if Uncertain]
(A single, specific, answerable question — no more than 3 sentences)
```

### Agent Log entry

A timestamped line is appended to the Notion Agent Logs page:
```
[2026-04-04 09:15 UTC] Filter: 'Paper Title' → Pass (Causal Relevance: High, Complexity: Low)
[2026-04-04 09:16 UTC] Filter: 'Paper Title' → Needs Review — question written to Filter Report
```

### `FilterResult` Pydantic model (Python API)

```python
class FilterResult(BaseModel):
    decision: Literal["pass", "reject", "uncertain"]
    reasoning_summary: str       # one paragraph → DB column
    full_report: str             # full Markdown → child page
    engineering_complexity: Literal["Low", "Medium", "High"]
    causal_relevance: Literal["High", "Medium", "Low", "None"]
    question_for_researcher: str # only populated when decision == "uncertain"
```

---

## Human-in-the-Loop

When the decision is `Uncertain`:

1. The agent sets `Status = Needs Review` and `Pass Initial Filter = Uncertain`
2. The Filter Report child page contains a clearly written question for the researcher
3. The CLI exits without blocking — other papers continue to be processed
4. The researcher reviews the Notion page, reads the question, and **manually sets** `Status` to either `Filter:Pass` or `Filter:Reject` directly in Notion
5. On the next `run-filter` or `run-pipeline` invocation, the agent ignores `Needs Review` papers (they are already decided by the human)

This means `Needs Review` is a terminal state for the filter agent — it will not re-process a paper in that status unless the researcher explicitly resets it to `Unprocessed`.

---

## Instruction File: `instructions/filter_agent.yaml`

The filter agent's behaviour is fully customisable through its instruction file. The researcher controls:

- `causal_relevance` — High / Medium / Low / None rubric with per-level signal lists
- `red_flags` — categories of negative signals (modality, architecture, scale, etc.)
- `engineering_complexity` — Low / Medium / High rubric with compute thresholds
- `decision_logic` — the Pass / Reject / Uncertain combination rules
- `output_instructions` — the exact section structure required in every Filter Report
- `examples` — few-shot examples of pass/reject/uncertain decisions

Changing this file changes the agent's judgment without touching any code.

---

## CLI

```bash
# Run the filter agent on all Unprocessed papers
python cli.py run-filter

# Run on a specific paper only (by arXiv ID or local ID)
python cli.py run-filter --paper-id "2310.01234"

# Check what is waiting to be filtered
python cli.py list --status "Unprocessed"

# Check what needs human review
python cli.py list --status "Needs Review"
```

---

## Configuration

In `config.yaml`:

```yaml
models:
  filter: "anthropic:claude-opus-4-6"   # model used for filter decisions

filter:
  use_full_text: true     # fetch full PDF for uncertain cases (Mode 2)
                          # set to false to always escalate uncertain → Needs Review
```

---

## Future Features

**Confidence score**
Rather than a hard three-way decision (pass/reject/uncertain), the agent could output a continuous confidence score (0–1) for each decision. The threshold for escalation to `Needs Review` versus auto-pass/reject would be configurable. This would give finer-grained control over how aggressive the filter is.

**Section-aware reading**
The current full-text mode reads the first N pages. A smarter approach would parse the PDF structure and extract specific sections — introduction, related work, conclusion — which are the most signal-rich for a filter decision. This avoids wasting tokens on methodology and experiment details that are not relevant at the filter stage.

**Batch abstract comparison**
When the database contains many papers, the agent could batch multiple abstracts into a single prompt and rank them by estimated causal relevance. This would be significantly cheaper than one LLM call per paper, at the cost of some precision. Suitable for initial triage of large paper dumps.

**Filter criteria versioning**
The `filter_agent.yaml` instruction file changes over time as the researcher refines their criteria. A future improvement would version-stamp each filter decision with the hash of the instruction file used, so that when criteria change, papers that were previously rejected can be re-evaluated with updated criteria.

**Explanation of near-misses**
When a paper is rejected, the current report only explains why. A future feature would additionally list what *would* need to be true for this paper to have passed — e.g., "would pass if the paper included a treatment-effect estimation component." This helps the researcher recognise follow-up work worth watching.

**Cross-paper coherence check**
After filtering a batch, the agent could run a second pass comparing the accepted papers as a group — flagging if two accepted papers are so similar that only one should proceed to brainstorming, or identifying clusters of accepted papers that suggest a broader research direction.

**Automatic re-filter on instruction update**
When `filter_agent.yaml` is modified, a hook could automatically identify all `Filter:Reject` papers and mark them `Unprocessed` so they are re-evaluated with the new criteria on the next run.
