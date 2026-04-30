---
name: coderabbit-triage
description: Thin Codex skill shim for node_coderabbit_triage. Use to classify CodeRabbit PR review threads through the OmniMarket runtime adapter.
---

# CodeRabbit Triage

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_coderabbit_triage` node. The node owns fetching review threads,
classifying CodeRabbit findings, acknowledging safe suggestions, and resolving
eligible threads. Do not add GitHub API calls, classification keywords,
reply text, or thread-resolution logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `repo` | GitHub repo slug such as `OmniNode-ai/omnimarket` | Required |
| `pr_number` | Pull request number to triage | Required |
| `correlation_id` | UUID v4 correlation id for the triage run | Generate when omitted |
| `dry_run` | Classify threads without replies or resolution | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "coderabbit_triage" \
  --payload '<json-payload>' \
  --timeout-ms 120000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Build the payload with this shape:

```json
{
  "repo": "OmniNode-ai/omnimarket",
  "pr_number": 465,
  "correlation_id": "<uuid-v4>",
  "dry_run": true
}
```

Map user inputs into the same field names. Generate `correlation_id` when the
user does not supply one.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_coderabbit_triage/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `coderabbit_triage`
- Runtime topic: `onex.cmd.omnimarket.coderabbit-triage-start.v1`
- Completion topic: `onex.evt.omnimarket.coderabbit-triage-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `total_threads`, `blocking_count`,
`suggestion_count`, `unknown_count`, `resolved_count`, and a concise thread
summary with severity, matched keyword, and URL when present. For `dry_run`,
make clear that no replies or thread resolution were performed.
