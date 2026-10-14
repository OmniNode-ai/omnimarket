Structural hard tier, 2026-10-14
================================

The tiers in `../2026-10-14/` rank prompt length only. This directory holds a
tier that is hard by structure instead, run on every pinned rung and on the
unpinned ladder, so the question "what makes the chain fail or escalate" can be
asked of three structural features separately.

Every number here is figured from the records beside it; nothing is typed by
hand. `tests/test_rung_results.py` fails when `features.txt` differs from the
table the records give.

The tasks
---------

`bundles_hard/` holds 20 tasks built by `build_bundles.py hard` from ten merged
omnimarket commits (listed in `HARD_COMMITS`). Each commit's own test passes at
the commit and fails when the commit's source files are put back to the
parent; the build checks both and refuses a commit whose test passes on the
parent. Each commit gives two tasks:

| id | class | the model is asked | scored by |
| -- | -- | -- | -- |
| H6-nn | code_generation | edits, as FILE / SEARCH / REPLACE blocks, that make the failing test pass | `multi_file_edit_and_test`: the edits are applied to the commit's tree with the parent's files, and the commit's test file is run in full |
| H3-nn | review | the FILE lines naming every shown source file that must change | `exact_match`: every file the fix needs, and none of the shown files the commit did not change |

The code_generation scorer runs the test on the files as shown before applying
anything; if it passes there the cell is a harness error, never a pass. A file
the commit changed but whose parent version alone does not fail the test is
neither required nor forbidden in the review answer.

Each bundle records its features under `scorer_config.features`:

| feature | how it is measured |
| -- | -- |
| files touched | source files the commit changed (1 to 3) |
| cross-file cause | true when none of the changed files is one the failing test imports |
| context size | characters of the prompt as fed; binned under 60k, 60k-150k, over 150k |

Size is padded with real sibling source files at the parent, toward targets of
none, 95k and 190k characters. The receipts measure these prompts at about 4.1
characters per token, so 95k is about 24k tokens and 190k about 48k tokens,
three quarters of the 65536 tokens local-coder declares. The target rotates independently of commit
order, with a different offset for the two classes, so size is not tied to
files touched or to cross-file cause. Where a commit's own files already
exceed the target, the prompt is as large as those files: task 02 is about
305k characters (about 74k tokens), beyond the local rungs' context whichever
target it drew, and task 03 is about 115k characters at its smallest. Task 02
is the only one past the local window.

What is here
------------

| path | what it holds |
| -- | -- |
| `<rung>.jsonl` | one record per task per rung, as the driver wrote it |
| `responses/<rung>/<task>.json` | the bytes each record was scored from |
| `table.txt` | accepted share per class and rung (the existing table) |
| `features.txt` | failure and escalation per feature level, all tasks and per class |

Reading features.txt
--------------------

- "pinned cells failed" counts cells whose answer the mechanical scorer did not
  pass, with no answer counted as a failure. "not accepted" also counts a
  correct answer the delegation's own quality gate refused.
- "tasks failed" counts tasks where fewer than half the pinned rungs that ran
  them produced a passing answer.
- "ladder escalated" is an unpinned run whose receipt tried more than one rung.
  "ladder left local" is one that tried any rung that is not local: routing
  sends an input larger than the local context straight to a cloud rung, which
  is one attempt and no escalation.
- Intervals are 95% Wilson; differences are last level minus first with a
  Newcombe interval. Cells of one task across rungs are not independent, so the
  cell intervals are narrower than the evidence; the task-level column is the
  conservative one.

Two things the deployed lane does that shape these numbers
-----------------------------------------------------------

- The code_generation quality gate accepts a code answer only on tests run
  against it. A delegated answer here carries no test run, so on the onex
  surface every code_generation answer is refused at the deterministic floor
  and the ladder's decision is terminate, not climb, whether the answer is
  right or not. The scorer still runs the saved answer, which is why "failed"
  and "not accepted" are reported apart. The GLM rungs run through the Claude
  Code harness and are not gated this way.
- The GLM harness has a 180 second call budget. A GLM cell that ran out of it
  is recorded as a timeout with no answer and is not run again.

Rerun commands
--------------

Rebuild the corpus (checks every commit's test both ways):

    uv run python -m benchmarks.delegation_ladder.build_bundles hard

One cell against a rung, and the whole hard tier on the unpinned ladder:

    uv run python -m benchmarks.delegation_ladder.run_ladder --rung local-qwen3.8-27b --only H6-04 --out-dir /tmp/cell
    uv run python -m benchmarks.delegation_ladder.run_ladder --rung unpinned-ladder --only H3-01 ... H6-10 --out-dir <dir>

Offline, from the saved responses, with no model call:

    uv run python -m benchmarks.delegation_ladder.run_ladder --rescore benchmarks/delegation_ladder/results/rungs/2026-10-14-hard
    uv run python -m benchmarks.delegation_ladder.run_ladder --table benchmarks/delegation_ladder/results/rungs/2026-10-14-hard --by-feature

Scrubbing
---------

The failure tails in the prompts carried the lab host's home and temporary
directories. After the runs those were rewritten in the committed bundles with
replacements of the same length (a placeholder user name in each path), so the
recorded `context_chars` is still the size each rung was fed. The shown source
files also carry text the public-repository hygiene and leaked-literal gates
refuse on added lines (private repository names, private addresses, lab lane
names); in the committed prompts each such span is masked with `x` characters
of the same length. The runs were fed the unmasked text, which is the text of
the public repository at those commits; the scorer runs against the
repository, so no verdict depends on the masking. Absolute lab
paths in the records are written as placeholders (`$OMNI_HOME`, `$HOME`,
`$LAB_RUN`) and the host as its lab label.

How the runs were made
----------------------

All six rungs ran on one Linux lab host on the deployed lane (GLM through the
harness), each task once, concurrency 4 on the onex rungs and 3 on GLM. A
plumbing failure (no receipt, a client timeout) is run once more with
`--rerun-infra` and its first attempt is kept under `first-attempt/`. The
records carry the driver commit they started from; the hard-corpus scorer and
builder were uncommitted on top of it when the runs started, and every saved
response was rescored with the committed scorer before commit.
