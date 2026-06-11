# OMN-12964 - Quality-gate calibration proof (P1.7)

**Status:** quality scores now demonstrably discriminate. Experiments 1-3
(OMN-12944 / P0.2) interpretation is unblocked.

> WARNING - experiment-validity prerequisite. Before this fix landed, the
> delegation quality gate returned a degenerate `{0.0, 1.0}` verdict and applied
> code-docstring DoD to the prose `document` task class. Every output scored
> **0.000 on both tiers** (live CID `a604cd40`). A scoring artifact masqueraded
> as an ON/OFF effect. **Experiment 1-3 results captured before this merge must
> not be trusted** - re-run or re-score against the calibrated gate.

## Root cause (two compounding defects)

1. **Scorer defect - degenerate distribution.**
   `handler_quality_gate.delta` returned `quality_score=0.0` on *any* failing
   DoD check and `1.0` only when every check passed. A near-perfect output and
   an outright refusal scored identically. The "score" was a binary verdict, not
   a graded signal - it could never discriminate.

2. **DoD-definition defect - wrong DoD for prose.**
   The `document` task class (capability `natural_language_generation`, i.e.
   prose) used the `documentation` (docstring) DoD: deterministic
   `docstring_present` + heuristics `follows_google_style`,
   `covers_args_returns_raises`. Those require code-docstring markers
   (`args:`/`returns:`/`raises:`), so any genuine prose failed deterministically.

## Fix

- `delta` now computes a **graded** `quality_score` = band-weighted fraction of
  DoD checks satisfied (deterministic band weighted above heuristic). The
  pass/fail gate is unchanged - deterministic failures still hard-block - but the
  score is now continuous and independent of the verdict, so downstream analysis
  can separate a near-miss from a total failure.
- `task_class_contracts.v1.yaml` `document` DoD corrected to prose-appropriate
  checks: deterministic `response_non_empty`, heuristic `no_refusal` + `accurate`.
  The docstring DoD stays on `documentation`, its correct home.

## Enforcement ratchet

- `tests/unit/delegation/test_quality_gate_calibration.py` - asserts the corpus
  yields a non-degenerate distribution, that known-good out-scores known-bad, and
  that prose `document` is not scored against docstring DoD.
- `scripts/ci/run_quality_gate_calibration.py` - same assertions as a CI gate,
  wired into `.github/workflows/ci.yml` ("Quality-gate calibration (enforce)").
  Fails the build (exit 1) on score collapse, insufficient range, band overlap,
  or corpus/contract DoD drift.
- `quality_gate_calibration_packet.json` - emitted evidence packet.

## Before vs after (calibration corpus)

| metric | before fix | after fix |
|--------|-----------|-----------|
| distinct scores | 1 (`{0.0}`) | 7 (`0.0 … 1.0`) |
| score range | 0.000 | 1.000 |
| mean(good) | 0.000 | 1.000 |
| mean(bad) | 0.000 | 0.543 |
| good > bad | no | yes (margin 0.457) |

See `quality_gate_calibration_packet.json` for the per-case after-fix scores.
