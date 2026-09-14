# OMN-18379 recorded delegation fixtures

Recorded verbatim from local `onex delegate` runs on 2026-09-14, task class
`document`, tier `local`, backend `local-heavy-reasoning`, model
`Qwen3.6-35B-A3B`. Nothing in these files is a secret; they are model output
and a receipt the CLI already writes to disk unencrypted.

| File | What it is |
| -- | -- |
| `43d269f5_leaked_preamble_unpaired_think.txt` | Run `43d269f5-d2ed-44cc-9338-04fdd2034d9a`. A 122-line reasoning scratchpad opening "Here's a thinking process:", closed by a trace terminator with **no matching opener**, then a correct JSON answer. The word "unverified" appears at line 31 — inside the scratchpad, never in the answer. This is the response the blocking rule `accurate` vetoed. |
| `43d269f5_refused_receipt.json` | That run's receipt, with the `capture_log` field removed (a console transcript, no decision content) and every local clone path rewritten to `$OMNI_HOME` so the file is portable. Three refused attempts, each recorded `acceptance_reason: score_below_required_bar` at `quality_score: 0.9` — against the `document` class bar of `0.8`. The label contradicts its own numbers. |
| `fd67cd3c_leaked_preamble_markdown_answer.txt` | Run `fd67cd3c-d175-470d-90da-f29c66847701`. Same leak shape, markdown answer. This is one of the two responses that broke the hourly Linear-sweep renderer, which read the scratchpad as a failed render. |
| `fd67cd3c_derived_no_trace_tag.txt` | **Derived**, not recorded: the file above with its single terminator line deleted. It exercises the markdown-header boundary rule, which the live corpus does not — every leak observed on this host carried a terminator. |
| `3d4dd739_leaked_preamble_plain_answer.txt` | Run `3d4dd739-a32b-41d5-8db4-8d896139e713`. Same leak shape, plain-prose answer with no markdown header and no fence. It is the case the structural boundaries could not resolve on their own. |

## What the live corpus does and does not prove

Every leaked preamble in the 86 recorded local runs on this host closes with an
unpaired trace terminator, so that rule is the one live data exercises. The
answer-marker, markdown-header and fenced-block rules are declared fallbacks
tested against synthetic and derived input. They are in the contract because a
model that emits no terminator is a live possibility, not because one was
observed.
