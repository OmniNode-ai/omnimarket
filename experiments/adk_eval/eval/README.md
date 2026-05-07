# ADK Evaluation — Labeled Sample

**LLM-self-labeled, not human-gold.** The 30 labels in `labeled_sample.yaml`
were assigned by an autonomous agent (Claude Opus 4.7) during the ADK
evaluation spike (Task P5 of
`docs/plans/2026-04-23-adk-evaluation-tech-debt-agent.md`).

## Methodology

1. Ran `uv run mypy src/ --strict --output json` across omnibase_core,
   omniclaude, and omnibase_infra (with tests/ scope for the last).
2. Combined into `input_findings.jsonl` (30 records).
3. Labeled each finding into one of four priorities:
   - `critical` — high blast radius, likely to cause runtime errors or erode
     platform-wide type safety.
   - `major` — real defect but scoped to one module/flow.
   - `minor` — genuine issue but low-impact / easily absorbed.
   - `noise` — cleanup only (unused ignores, optional imports, test-file
     variable-as-type).

## Scope expansion

omnibase_core alone produced only 4 mypy --strict findings on 2026-04-23.
The plan's P5 stop condition called out this possibility and authorized
widening to additional repos. Final scope: omnibase_core + omniclaude +
omnibase_infra tests.

## Why this is NOT human-gold

- One agent wrote both the labels and (later) ran the scorer. F1 signal is
  directional; it cannot separate "agent agrees with itself" from "track
  outperforms".
- No second reviewer; labels reflect the labeler's biases (e.g., a probable
  over-indexing on "unused-ignore is always noise").
- No disagreement surface; a real gold rubric would show inter-rater
  agreement.

Use this file as a triage rubric for the spike. Do not cite the resulting
F1 numbers as a quality claim against either track.
