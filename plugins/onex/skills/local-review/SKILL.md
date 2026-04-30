---
name: local-review
description: Thin Codex skill shim for node_local_review. Use to run the OmniMarket local review loop through the runtime adapter.
---

# Local Review

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_local_review` node. The node owns the review, fix, commit,
clean-check, and completion loop. Do not add review heuristics, file scanning,
fix logic, commit logic, or shell loops to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `correlation_id` | UUID v4 correlation id for the review run | Generate when omitted |
| `max_iterations` | Maximum review-fix cycles before stopping | `10` |
| `required_clean_runs` | Consecutive clean runs required before done | `2` |
| `dry_run` | Run without side effects | `false` |
| `requested_at` | ISO-8601 request timestamp | Current UTC time |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "local_review" \
  --payload '<json-payload>' \
  --timeout-ms 300000
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
  "correlation_id": "<uuid-v4>",
  "max_iterations": 10,
  "required_clean_runs": 2,
  "dry_run": false,
  "requested_at": "<utc-iso-8601-timestamp>"
}
```

Map user inputs into the same field names. Generate `correlation_id` and
`requested_at` when the user does not supply them.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_local_review/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `local_review`
- Runtime topic: `onex.cmd.omnimarket.local-review-start.v1`
- Completion topic: `onex.evt.omnimarket.local-review-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `final_phase`, `iteration_count`,
`issues_found`, `issues_fixed`, and `error_message`. For dry runs, report the
planned review-loop state without inventing findings or fixes.
