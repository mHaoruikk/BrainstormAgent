# Brainstorming Pipeline

## Overview

After a paper passes the initial filter (`Status = Filter:Pass`), the brainstorming pipeline
runs one or more iterative brainstorm/critique loops. The configured brainstorm models and
critic models are both lists. Every brainstorm model is paired with every critic model, and
each pair runs an independent refinement loop of up to `brainstorm.max_rounds` rounds.

The brainstorm model returns a strictly structured `BrainstormResult`, and the application
assembles the proposal markdown from those fields before passing it to the critic or writing
it to Notion.

Each round produces one combined Notion child page containing:

- run metadata (round number, brainstorm model, critic model)
- the brainstorm proposal assembled from the structured `BrainstormResult` fields
- the critique (`CritiqueResult.full_report`)

After all configured model pairs finish, the pipeline selects the strongest final critique,
writes aggregate scores back to the database, and advances the paper's status.

---

## Agents

| Agent | Config | Role |
|---|---|---|
| `BrainstormAgent` | `config.models.brainstorm[*]` | Produces exactly one research proposal per round |
| `CritiqueAgent` | `config.models.critic[*]` | Critiques the proposal and scores it |

The pipeline is multi-model, but the output schema is still the single-proposal schema defined
in `models/paper.py`.

---

## Data Models

### `BrainstormResult`

The brainstorm agent returns one structured proposal with:

- `paper_summary`
- `response_to_critique`
- `title`
- `description`
- `novelty_rationale`
- `solution_sketch`
- `experiment_plan`
- `open_questions`

Semantics:

- `response_to_critique` is empty on round 1
- `response_to_critique` is populated on round `N > 1` with a short summary of what changed
- there is no brainstorm `full_report` field anymore; markdown is assembled in code

This is a single proposal, not a list of research angles or a free-form markdown blob.

### `CritiqueResult`

The critic returns:

- `novelty_score`
- `viability_score`
- `contribution_score`
- `causal_rigor_score`
- `strengths`
- `weaknesses`
- `recommendation`
- `critique_summary`
- `full_report`

---

## Interaction Protocol

For each `(brainstorm_model, critic_model)` pair:

1. Gather paper context from Notion.
2. Run the brainstorm agent for Round 1.
3. Run the paired critic on the brainstorm output.
4. Assemble the brainstorm markdown report from the structured fields.
5. Write a combined Notion child page for that round.
6. If the critic meets the configured score thresholds, stop early for that pair.
7. Otherwise, feed prior proposal/critique rounds back into the next brainstorm call.
8. Stop after `brainstorm.max_rounds` even if thresholds are still not met.

All model pairs run independently. The final paper-level DB update uses the strongest final
critique across all pairs.

---

## Early Stop Logic

Early stop is threshold-based, not recommendation-based.

The pair stops when:

- `novelty_score >= config.thresholds.min_novelty_rating`
- `viability_score >= config.thresholds.min_viability_rating`

If no pair reaches the threshold after the configured number of rounds, the paper ends in
`Status = Needs Review`.

---

## Paper Context Given to BrainstormAgent

The brainstorm agent currently builds its user message from:

1. `Abstract` property from the paper row
2. prior brainstorm session pages when running with `--rerun`
3. full paper text extracted from the attached PDF or PDF/ArXiv URL when available
4. child page `Math Summary`
5. child page `Filter Report`

If `--rerun` is used, prior brainstorm round pages are compressed into a single context block
and the new round numbering is offset so subsequent child-page titles continue from the
existing rounds.

---

## Round Messages

### Brainstorm Round 1

The user message contains the paper context, asks for exactly one proposal, and restates
the exact `BrainstormResult` field contract at the end of the prompt.

The restated contract requires exactly these fields:

- `paper_summary`
- `response_to_critique` as an empty string
- `title`
- `description`
- `novelty_rationale`
- `solution_sketch`
- `experiment_plan`
- `open_questions`

### Brainstorm Round N > 1

The user message contains:

- the paper context
- all prior brainstorm reports for that pair
- all prior critique reports for that pair
- an explicit instruction to refine the proposal and explain what changed
- the exact `BrainstormResult` field contract restated at the end of the prompt

For refinement rounds, the contract specifically requires `response_to_critique` to contain
a short summary of the changes made in response to the prior critique.

### Critique Round N

The critic receives:

- the current brainstorm proposal
- the immediately prior critique when `N > 1`
- an instruction to reassess what was fixed and what remains weak

---

## Notion Storage Layout

The pipeline writes one combined child page per `(brainstorm_label, critic_label, round)`:

`Brainstorm — {brainstorm_label} + {critic_label} — Round {n}`

Each page contains:

- `## Run Metadata`
- `## Proposal`
- `## Critique`

The assembled `## Proposal` section has this structure:

- `## Paper Summary`
- optional `## Response to Critique`
- `1. Title`
- `2. Problem Statement`
- `3. Motivation & Hypothesis`
- `4. Proposed Method`
- `5. Experiment Plan`
- `**Open Questions**`

At the paper-row level, the pipeline updates:

- `Novelty Rating` as a Number
- `Viability Rating` as a Number
- `Critique Summary` as rich text
- `Status` as `Critiqued` or `Needs Review`

---

## Config

```yaml
models:
  brainstorm:
    - model: "anthropic:claude-opus-4-6"
      label: "Claude"
  critic:
    - model: "openai:gpt-4o"
      label: "GPT-4o"

brainstorm:
  max_rounds: 3
```

In `config.py`:

- `models.brainstorm` is `list[LabeledModelConfig]`
- `models.critic` is `list[LabeledModelConfig]`
- `brainstorm.max_rounds` controls the per-pair loop length

Today the project may be configured with one brainstorm model and one critic model, but the
runtime shape is already list-based.

---

## Status Transitions

```text
Filter:Pass / Brainstorming
    -> BrainstormPipeline.run()
    -> Critiqued     (best final critique meets thresholds)
    -> Needs Review  (no pair meets thresholds after max rounds)
```

The pipeline also accepts papers already in `Brainstorming`, and with `--rerun` it also accepts
papers in `Critiqued` or `Needs Review` so interrupted or low-quality runs can be re-brainstormed.
