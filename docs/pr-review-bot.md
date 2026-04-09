# PR Review Bot (`node_pr_review_bot`)

Automated PR review with multi-model fan-out, GitHub thread posting, and judge
model verification before threads can be resolved.

**Related tickets**: OMN-7963 (epic), OMN-7964–OMN-7973

---

## Architecture Overview

### Problem

GitHub's `requiresConversationResolution` branch protection blocks merge until
all review threads are resolved — but any contributor can dismiss a thread by
posting "done" without actually fixing the issue. There is no independent
verification of whether the fix is legitimate.

### Solution

`node_pr_review_bot` is an omnimarket workflow node that:

1. Triggers on every PR via GitHub Actions.
2. Reads the full diff and fans out to reviewer LLM models (via
   `node_hostile_reviewer`).
3. Posts one GitHub review thread per MAJOR or CRITICAL finding.
4. When a thread is resolved, a separate **judge model** verifies the fix
   before the thread stays resolved.
5. If the judge rejects the fix, the thread is re-opened and the PR stays
   blocked.

### Component Map

```
node_pr_review_bot/
├── handlers/
│   ├── handler_fsm.py          # Pure COMPUTE FSM — phase transitions, circuit breaker
│   └── handler_diff_fetcher.py # Side-effect: fetches and parses the PR unified diff
│   # Parallel PRs (OMN-7969–OMN-7972) add:
│   # handler_thread_poster.py  — posts review threads via GitHub API
│   # handler_thread_watcher.py — polls for thread resolution events
│   # handler_judge_verifier.py — calls judge model to verify each resolution
│   # handler_report_poster.py  — posts final verdict summary as PR comment
├── adapter_github_bridge.py    # Adapter: all GitHub REST API calls (rate-limit aware)
├── contract.yaml               # Node contract — topics, inputs, outputs, handler routing
├── models/models.py            # Pydantic models (DiffHunk, ReviewFinding, ThreadState, …)
├── node.py                     # ONEX node entry point
└── workflow_runner.py          # Wires FSM + concrete sub-handlers for local execution
```

### FSM and Sub-handlers

`HandlerPrReviewBot` is a **pure COMPUTE** state machine. It contains no I/O.
All side-effectful operations are injected as protocol implementations:

| Protocol | Concrete class | Role |
|---|---|---|
| `ProtocolDiffFetcher` | `HandlerDiffFetcher` | Fetch and parse the PR unified diff |
| `ProtocolReviewer` | `HandlerHostileReviewerAdapter` (OMN-7971) | Fan-out to reviewer models |
| `ProtocolThreadPoster` | `HandlerThreadPoster` (OMN-7969) | Post GitHub review threads |
| `ProtocolThreadWatcher` | `HandlerThreadWatcher` (OMN-7970) | Poll for thread resolutions |
| `ProtocolJudgeVerifier` | `HandlerJudgeVerifier` (OMN-7971) | Call judge model for each resolution |
| `ProtocolReportPoster` | `HandlerReportPoster` (OMN-7972) | Post final summary PR comment |

The `AdapterGitHubBridge` abstracts all GitHub REST API calls behind a protocol,
handling authentication, pagination, rate-limit backoff, and dedup checks.

### GitHub Thread Protocol

Each MAJOR or CRITICAL finding produces one review thread:

```
**[PR-BOT] Finding: {title}** (severity: {severity}, confidence: {confidence})

{description}

📍 File: `{file_path}`, lines {start}–{end}

**Resolution required before merge.** This thread will be verified by the judge
model before it can be dismissed. Post a reply explaining the fix and tag
`@omnibot-judge verify`.

<!-- omnibot:finding:{finding_id} -->
```

NITs and MINOR findings are bundled into a single summary comment so they do
not inflate the required-resolution count.

### Judge Verification Protocol

When a thread is resolved:

1. `HandlerThreadWatcher` detects the resolution event.
2. `HandlerJudgeVerifier` sends the judge model:
   - The original finding (title, description, evidence, file/line range)
   - The full thread conversation (all replies)
   - The current diff at the referenced file and line range
3. Judge returns structured JSON: `{"verdict": "PASS" | "FAIL", "reasoning": "..."}`
4. **PASS**: thread stays resolved; finding marked `VERIFIED_PASS`.
5. **FAIL**: bot posts a reply explaining the rejection and re-opens the thread; PR stays blocked.

### Model Assignment

| Role | Model | Endpoint env var | Notes |
|---|---|---|---|
| Primary reviewer | Qwen3-Coder-30B-A3B | `LLM_CODER_URL` | 64K context; code-specialist |
| Secondary reviewer | Qwen3-14B-AWQ | `LLM_CODER_FAST_URL` | Faster second opinion |
| Judge | DeepSeek-R1-Distill-Qwen-32B | `LLM_DEEPSEEK_R1_URL` | Reasoning model; different from build-loop models |
| Judge fallback | Qwen3-Next-80B-A3B | port `:8102` | Used when R1 is unavailable or verdict is ambiguous |

**Critical invariant**: the judge model must never be the same model that
generated the code. Build-loop agents use `LLM_CODER_URL` and
`LLM_CODER_FAST_URL`. The judge is restricted to `LLM_DEEPSEEK_R1_URL` or
port `:8102`.

---

## FSM State Diagram

```
         ┌──────────────────────────────────────────────────────────────┐
         │                                                              │
  [INIT] ──► [FETCH_DIFF] ──► [REVIEW] ──► [POST_THREADS] ──► [WATCH] ──► [JUDGE_VERIFY] ──► [REPORT] ──► [DONE]
                  │               │               │                │              │                │
                  │               │               │                │              │                │
                  └───────────────┴───────────────┴────────────────┴──────────────┴────────────────┘
                                                        │
                                          failure (retry same phase)
                                                        │
                                          3 consecutive failures
                                                        │
                                                   [FAILED]
```

**Circuit breaker**: any phase that fails 3 consecutive times transitions to
`FAILED` instead of retrying. The `advance()` method on `HandlerPrReviewBot`
increments `consecutive_failures` on each failure and resets to `0` on success.

**Phase details**:

| Phase | Handler | Output stored in FSM state |
|---|---|---|
| `INIT` | (no-op) | — |
| `FETCH_DIFF` | `HandlerDiffFetcher` | `diff_hunks: tuple[DiffHunk, ...]` |
| `REVIEW` | `ProtocolReviewer` | `findings: tuple[ReviewFinding, ...]` |
| `POST_THREADS` | `ProtocolThreadPoster` | `thread_states: tuple[ThreadState, ...]` |
| `WATCH` | `ProtocolThreadWatcher` | `thread_states` (updated) |
| `JUDGE_VERIFY` | `ProtocolJudgeVerifier` | `thread_states` (verified) |
| `REPORT` | `ProtocolReportPoster` | — |
| `DONE` / `FAILED` | (terminal) | — |

---

## Configuration

### Required Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | Yes | GitHub PAT or Actions token. Must have `pull-requests: write` scope. |
| `LLM_CODER_URL` | Yes | Primary reviewer endpoint (Qwen3-Coder-30B-A3B, port 8000). |
| `LLM_CODER_FAST_URL` | Yes | Secondary reviewer endpoint (Qwen3-14B-AWQ, port 8001). |
| `LLM_DEEPSEEK_R1_URL` | Yes | Judge model endpoint (DeepSeek-R1, port 8101). |

Set via `~/.omnibase/.env` or GitHub Actions repository variables (never hardcoded).

### Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GITHUB_API_BASE` | `https://api.github.com` | Override for GitHub Enterprise. |

### Node Input Parameters

Passed as command payload (`ReviewRequest`) when invoking the node:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pr_number` | int | required | GitHub PR number. |
| `repo` | str | required | GitHub repo in `owner/repo` format. |
| `reviewer_models` | list[str] | `["qwen3-coder-30b", "qwen3-14b"]` | Reviewer model identifiers. |
| `judge_model` | str | `"deepseek-r1"` | Judge model identifier. Must not be a build-loop model. |
| `severity_threshold` | str | `"major"` | Minimum severity to post a review thread (`major` or `critical`). |
| `dry_run` | bool | `false` | Run without posting any GitHub comments. |
| `max_findings_per_pr` | int | `20` | Cap on threads per PR; prevents spam on large diffs. |

### Node Output Fields

| Field | Type | Description |
|---|---|---|
| `verdict` | str | `clean`, `risks_noted`, or `blocking_issue` |
| `total_findings` | int | Total findings across all reviewer models. |
| `threads_posted` | int | Number of GitHub review threads posted. |
| `threads_verified_pass` | int | Threads that passed judge verification. |
| `threads_verified_fail` | int | Threads that failed judge verification (PR blocked). |
| `threads_pending` | int | Threads awaiting verification. |
| `judge_model_used` | str | Judge model endpoint used. |
| `duration_ms` | int | Total workflow duration in milliseconds. |

### Kafka Topics

Declared in `contract.yaml` — never hardcoded in handler code.

```
subscribe: onex.cmd.omnimarket.pr-review-bot-start.v1

publish:
  onex.evt.omnimarket.pr-review-bot-phase-transition.v1
  onex.evt.omnimarket.pr-review-bot-thread-posted.v1
  onex.evt.omnimarket.pr-review-bot-thread-verified.v1
  onex.evt.omnimarket.pr-review-bot-completed.v1
```

---

## Usage

### GitHub Actions (recommended)

Add to each repo that should be covered:

```yaml
# .github/workflows/pr-review-bot.yml
on:
  pull_request:
    types: [opened, synchronize, reopened]
  pull_request_review_comment:
    types: [created]

permissions:
  pull-requests: write   # required — GITHUB_TOKEN is read-only by default (R1)
  contents: read

jobs:
  pr-review-bot:
    runs-on: self-hosted  # .201 runner has LAN access to inference endpoints
    steps:
      - uses: actions/checkout@v4
      - name: Run PR Review Bot
        run: |
          uv run onex run node_pr_review_bot -- \
            --pr-number ${{ github.event.pull_request.number }} \
            --repo ${{ github.repository }}
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          LLM_CODER_URL: ${{ vars.LLM_CODER_URL }}
          LLM_CODER_FAST_URL: ${{ vars.LLM_CODER_FAST_URL }}
          LLM_DEEPSEEK_R1_URL: ${{ vars.LLM_DEEPSEEK_R1_URL }}
```

The self-hosted runner on `.201` provides direct LAN access to all inference
endpoints without tunneling.

### CLI (local or headless)

```bash
# Source credentials first
source ~/.omnibase/.env

# Dry run — no GitHub posts
uv run onex run node_pr_review_bot \
  --pr-number 123 \
  --repo OmniNode-ai/omnimarket \
  --dry-run

# Full review with custom judge model
uv run onex run node_pr_review_bot \
  --pr-number 123 \
  --repo OmniNode-ai/omnimarket \
  --judge-model deepseek-r1 \
  --severity-threshold major
```

### Skill Wrapper

```bash
/onex:pr_review_bot
```

Triggers a manual re-review of the current PR. Use after significant rework
when a fresh review pass is needed.

### Judge Re-verification Trigger

Post the following comment in a review thread to request re-verification after
addressing a finding:

```
@omnibot-judge re-verify
```

Rate limit: maximum 3 re-verification requests per finding per PR (R6).

---

## Quality Gate Enforcement

The enforcement chain requires no human reviewer action to function:

1. PR opened → bot runs → posts MAJOR/CRITICAL findings as review threads.
2. GitHub `requiresConversationResolution = true` (set via branch protection on all repos).
3. Author addresses finding → posts reply → bot's judge verifier runs.
4. Judge **PASS** → bot confirms resolution; thread marked resolved.
5. Judge **FAIL** → bot re-opens thread with rejection reason; PR stays blocked.
6. All threads PASS → bot posts final summary → PR unblocked.

Human reviewers can still review and approve separately. The bot gate is
additive, not a replacement for human review.

---

## Troubleshooting

### Bot posts no threads after running

- Check that `GITHUB_TOKEN` has `pull-requests: write` scope. The Actions
  `GITHUB_TOKEN` is read-only by default — the workflow must declare
  `permissions: pull-requests: write` (R1).
- Run with `--dry-run` first and inspect logs. If no findings are produced,
  the reviewer models may not be reachable — verify `LLM_CODER_URL` and
  `LLM_CODER_FAST_URL` are accessible from the runner host.
- Check that the PR targets `main`. The bot skips PRs targeting non-main
  branches to avoid noise on WIP branches (R9).

### Duplicate threads on the same finding

This should not happen: the `AdapterGitHubBridge.find_bot_thread_for_finding()`
method checks for an existing bot thread (identified by the
`<!-- omnibot:finding:{finding_id} -->` marker) before posting (R10). If
duplicates appear:

1. Confirm the `bot_login` parameter matches the GitHub login of the token used.
2. Check that the PR was not force-pushed between runs — force-push invalidates
   existing threads and the bot re-runs on the new commit (R5).

### Judge verification is slow or times out

The DeepSeek-R1 model on the M2 Ultra (port 8101) has a cold-start latency of
10–30 seconds. The `HandlerJudgeVerifier` timeout must be at least 90 seconds
(R4). If the judge endpoint is unavailable, the bot falls back to port `:8102`.
Check judge availability:

```bash
curl http://192.168.86.200:8101/health
```

### Judge returns malformed JSON

The verifier handles parse failures as `FAIL` with a clear error message posted
to the thread (R7). The thread will show:

```
Judge verification failed: could not parse model response as verdict JSON.
Treating as FAIL. Please address the original finding and re-tag @omnibot-judge verify.
```

This is expected behaviour on model instability. Re-trigger with
`@omnibot-judge re-verify` after the model recovers.

### Rate limit exhausted

The `AdapterGitHubBridge` checks `X-RateLimit-Remaining` on every response and
logs a warning when it drops below 50 (R8). On 429 or 5xx responses, the
adapter retries up to 3 times with exponential backoff. If rate limits are
exhausted on high-volume repos, consider:

- Using a PAT instead of `GITHUB_TOKEN` (5000 req/hr vs. 1000 req/hr).
- Increasing `max_findings_per_pr` cap downward to reduce API calls per run.

### Circuit breaker tripped (FAILED phase)

The FSM trips to `FAILED` after 3 consecutive failures in the same phase. Check
the `error_message` field in the `pr-review-bot-completed` Kafka event and the
runner logs. Common causes:

| Phase | Likely cause |
|---|---|
| `FETCH_DIFF` | `GITHUB_TOKEN` missing or invalid |
| `REVIEW` | Reviewer LLM endpoint unreachable |
| `POST_THREADS` | Token missing `pull-requests: write` scope |
| `WATCH` | GitHub API rate limit exhausted |
| `JUDGE_VERIFY` | Judge model endpoint down or timing out |
| `REPORT` | Token missing `pull-requests: write` scope |

Fix the underlying cause and re-trigger with `/onex:pr_review_bot`.
