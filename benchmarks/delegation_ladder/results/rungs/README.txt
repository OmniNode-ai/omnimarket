Rung benchmark results, 2026-10-14
=================================

Every number in the tables here is figured from the records beside it by
`benchmarks/delegation_ladder/summarise_results.py`; nothing is typed by hand.
`tests/test_rung_results.py` fails when a table differs from its records or a
saved response no longer rescores to its recorded verdict.

What is here
------------

| path | what it holds |
| -- | -- |
| `2026-10-14/<rung>.jsonl` | one record per task per pinned rung, as the driver wrote it |
| `2026-10-14/responses/<rung>/<task>.json` | the bytes each record was scored from |
| `2026-10-14/table.txt` | accepted share and 95% Wilson interval per class and rung, with the separation verdict |
| `2026-10-14/tiers.txt` | the same by difficulty tier for the test and code_generation classes, and the tier where each rung starts failing |
| `2026-10-14-ladder/` | the hard tier of both classes run once through the unpinned ladder, with each run's trace (`ladder.txt`) |
| `2026-10-14-ladder-easy-medium/` | the same run over the easy and medium tiers |
| `2026-10-14-glm-timeout-rerun/` | a side measurement: the 30 test and code_generation cells whose first GLM attempt hit the harness's call budget (23 on glm-5.3-flash, 7 on glm-5.3), run once more at concurrency 2; merged into no table |
| `2026-10-14-first-attempts/` | the first attempt of every plumbing failure that was run once more, kept apart from the results |

Which rungs ran which tasks
---------------------------

All five pinned rungs ran the 52 test and code_generation tasks (26 each): the
lab Qwen3.8-27B, glm-5.3-flash, glm-5.3, nemotron-3-ultra and nemotron-3-super.
The lab Qwen and both GLM rungs were also run over the other 83 tasks
(document, review, reasoning); those records are not committed here.

The two Gemini rungs are declared in the driver but were not run: their free
tier allows 20 requests per model per day, far below a full set.

Difficulty tiers
----------------

A tier is a function of the prompt length, fixed per class
(`DIFFICULTY_THRESHOLDS` in `summarise_results.py`). It is the only spread the
committed tasks have: every test task declares 3 dependent steps and every
code_generation task 4, and every task is scored by running real tests. A tier
here ranks the size of the input a rung is fed. It does not claim to rank
intrinsic difficulty, and `tiers.txt` shows where the two come apart.

Rerun commands
--------------

One cell, against the model (the rung's own surface and pin, no fallback):

    uv run python -m benchmarks.delegation_ladder.run_ladder --rung glm-5.3 --only R6-12 --out-dir /tmp/cell

One cell, offline, from the saved response with no model call (every cell of
the directory, non-zero exit on a mismatch):

    uv run python -m benchmarks.delegation_ladder.run_ladder --rescore benchmarks/delegation_ladder/results/rungs/2026-10-14

The tables:

    uv run python -m benchmarks.delegation_ladder.run_ladder --table benchmarks/delegation_ladder/results/rungs/2026-10-14
    uv run python -m benchmarks.delegation_ladder.run_ladder --table benchmarks/delegation_ladder/results/rungs/2026-10-14 --by-difficulty

The unpinned ladder over the hard tier (no backend pin; routing decides):

    uv run python -m benchmarks.delegation_ladder.run_ladder --rung unpinned-ladder --only <task ids> --out-dir <dir>

How the runs were made
----------------------

Each rung ran on the lab host that could reach it: the lab Qwen on the first
lab host, the GLM rungs through the Claude Code harness on the other two (the
tasks split by shard), the free OpenRouter rungs on the two Linux lab hosts, and
the unpinned ladder on a Linux lab host, because the Mac host cannot reach the
deployed lane's broker. Each record names its host and the driver commit.

A task is run once per rung. A failure of the plumbing (no receipt, a lane wait
that expired, a client timeout, or the runtime shutting down under the
delegation) is run once more with `--rerun-infra`; the first attempt is
archived and the rerun stands. A rung that is merely slow (the GLM harness's
180 second call budget) is its own result and is not run again.

A pinned onex row whose receipt says the ladder moved it to another backend is
counted in `pin moved` and never as that rung's accepted answer.

Scrubbing
---------

Absolute lab paths and addresses in the records (`command`, `receipt`, `detail`)
are written as placeholders (`$OMNI_HOME`, `$LAB_RUN`, `$LAB_UNIT`, `$HOME`), so
the committed files carry no lab topology. In four saved responses (glm-5.3,
nemotron-super and nemotron-ultra on R4-18, the unpinned ladder on R4-07) the
model had written a `/home/...` path into its own test fixture; it is written
`/srv/...` there, the `response_sha256` of those four cells was recomputed, and
each cell was rescored to the verdict it was recorded with before this was
committed.

Host names in the records are the lab-run host labels: h200 is the Mac host,
h201 and h202 are the two Linux GPU hosts. A local backend id that carried the
second Linux host's name is written `local-h202-chat`.
