# G2 Mass-Produce — Mechanical Scanner Long-Tail Provenance (OMN-13294)

This document is the durable evidence for the G2 mass-production batch of the
validator-standardization plan (§5 G2): mechanical single-file scanner validators
produced through the **proven** `node_generation_consumer` generation path, each
**corpus-accepted** against the live local model before any bake.

G1 (OMN-13293) proved the generate→corpus-accept loop on one canary
(hardcoded-absolute-path). The first G2 producer PR (#1312, merged to dev as
`113ad6bb`) added the first mechanical scanner (hardcoded-private-ip) plus the
reusable corpus registry + driver. This batch extends the registry with the
remaining mechanical scanner long-tail from the same proven invariants.

## What was mass-produced (this batch)

| Validator | Invariant ground truth | Attempts | Corpus (violation + clean) | Verdict |
|---|---|---|---|---|
| `hardcoded-localhost-url` | `node_aislop_sweep` `_HARDCODED_CONFIG_PATTERNS` (localhost + 127.0.0.1 URL) | 2 | 5 + 5 | ACCEPTED |
| `hardcoded-topic-string` | `node_aislop_sweep` `_HARDCODED_TOPIC_PATTERN` (`onex.<a>.<b>.<c>`) | 1 | 5 + 4 | ACCEPTED |
| `todo-fixme-marker` | `node_aislop_sweep` `_TODO_PATTERN` (`\b(TODO\|FIXME\|HACK)\b`) | 1 | 5 + 4 | ACCEPTED |

Each corpus carries adversarial mutation cases (`ModelCorpusFixture.mutation_of`),
so an all-curated corpus is rejected by `evaluate_corpus_acceptance` before the
model is even called (OMN-13289 guard). The per-run provenance JSON is committed
alongside this file (`<validator>.generation.json`).

## Generation run facts (live, identical routing for all three)

- provider: `local`
- model_id: `Qwen3.6-35B-A3B`
- routing_source: `contract` (model resolved by the routing authority from the
  contract `model_routing` + bifrost overlay keyed by `endpoint_ref: local-coder`
  — the generator never selects its own model)
- resolved_endpoint: the local-coder backend, recorded verbatim in each JSON as
  routing-authority proof
- usage_source: `measured` (real provider-reported token usage)
- corpus_checked = corpus_passed = `true`

## Acceptance authority — the corpus, not the LLM

A generated scanner is ACCEPTED only when `benchmark.corpus_checked and
benchmark.corpus_passed`: it flagged **every** `violation_fixture` (>=1 finding)
and produced **zero** findings on **every** `clean_fixture`, by deterministic
execution in the hardened sandbox (no filesystem / network / env / clock). The
LLM self-report is never the authority (memory `feedback_adversarial_receipts`).

## Negative control — acceptance is meaningful, not always-true

Each new corpus was proven to REJECT a deliberately-broken handler, in both
failure directions, and to ACCEPT a correct hand-authored reference scanner. This
is encoded permanently in
`tests/unit/nodes/node_generation_consumer/test_validator_corpora.py`:

- `test_corpus_rejects_a_flag_nothing_scanner` — a permissive gate that flags
  nothing misses every violation_fixture → REJECTED (false-negative direction).
- `test_corpus_rejects_a_flag_everything_scanner` — an over-eager gate that flags
  everything false-flags every clean_fixture → REJECTED (false-positive direction).
- `test_longtail_corpus_accepts_a_correct_reference_scanner` — a correct stdlib-`re`
  reference scanner passes the corpus → ACCEPTED (corpus is satisfiable, not
  mis-specified).

A standalone replay of the captured accepted handlers (re-fed through
`evaluate_corpus_acceptance`) reproduces score `1.0` for all three, confirming the
generation→acceptance result is deterministic and replayable
(`negative-control.acceptance.json`).

## Staging — shadow, NOT blocking (deliberate)

These validators are PRODUCED and corpus-accepted; they are **not** yet wired as
blocking gates. Per the plan, the bake is staged shadow → blocking and untested
validators are not blanket-blocked. Concretely:

- `architecture-handshakes/validator-requirements.yaml` declares each as the
  fleet requirement (the target end-state: pre_commit + ci_workflow required).
- `architecture-handshakes/validator-requirements-baseline.yaml` records each as
  an accepted `backlog` (shadow) gap for omnimarket — the owner is omnibase_core,
  consumed via OMN-9050 pin-bump propagation. The validator-requirements consumer
  confirms `baseline-clean` (the recorded gaps exactly match the live scan; no new
  unrecorded gap, no stale entry).

## Deferred (the bake step — explicitly out of this batch)

The following per-validator work is the consumer-side bake, deliberately deferred
so no untested validator is blanket-blocked:

1. Hand-land each accepted handler in `omnibase_core` (the owner of build-time
   validators), mirroring the G1 `omnibase_core/.../validation/local_paths/`
   package layout (`runtime_<name>.py` + `handler.py` + `__init__.py` + a per-
   validator `GENERATION_PROVENANCE.md`) with the captured `handler_source`.
2. Shadow-bake each on real fleet code (zero false positives vs the ground-truth
   regex), as the local_paths canary did over 6,999 files.
3. Flip each gate to blocking (`required` in baseline classification, remove the
   gap entries) only after its shadow audit clears.

Producer != owner: this batch is the producer side (corpus + driver + corpus-
accepted evidence) landing in omnimarket. The core landing + shadow-bake +
blocking flip are tracked as the follow-on consumer step.
