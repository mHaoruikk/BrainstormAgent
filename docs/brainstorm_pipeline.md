# Brainstorming Pipeline

## Overview

After a paper passes the initial filter (`Status = Filter:Pass`), the brainstorming pipeline
runs one or more iterative brainstorm/critique loops. The configured brainstorm models and
critic models are both lists. Every brainstorm model is paired with every critic model, and
each pair runs an independent refinement loop of up to `brainstorm.max_rounds` rounds.

Each round produces one combined Notion child page containing:

- run metadata (round number, brainstorm model, critic model)
- the brainstorm proposal (`BrainstormResult.full_report`)
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
- `title`
- `description`
- `novelty_rationale`
- `solution_sketch`
- `experiment_plan`
- `open_questions`
- `full_report`

This is a single proposal, not a list of research angles.

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
4. Write a combined Notion child page for that round.
5. If the critic meets the configured score thresholds, stop early for that pair.
6. Otherwise, feed prior proposal/critique rounds back into the next brainstorm call.
7. Stop after `brainstorm.max_rounds` even if thresholds are still not met.

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
2. full paper text extracted from the attached PDF or PDF/ArXiv URL when available
3. child page `Math Summary`
4. child page `Filter Report`

---

## Round Messages

### Brainstorm Round 1

The user message contains the paper context and asks for exactly one proposal.

### Brainstorm Round N > 1

The user message contains:

- the paper context
- all prior brainstorm reports for that pair
- all prior critique reports for that pair
- an explicit instruction to refine the proposal and explain what changed

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

The pipeline also accepts papers already in `Brainstorming`, so interrupted runs can be retried.
