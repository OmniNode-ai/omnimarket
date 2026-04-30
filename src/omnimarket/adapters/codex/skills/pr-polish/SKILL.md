---
name: pr-polish
description: Thin Codex skill shim for node_pr_polish. Use to polish a PR toward merge readiness through the OmniMarket runtime adapter.
---

# PR Polish

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_pr_polish` node. The node owns conflict resolution, CI repair,
review-comment handling, local review, push, and automerge behavior. Do not add
GitHub scripting, shell repair loops, local-review logic, or merge logic to this
skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `repo` | GitHub repo slug such as `OmniNode-ai/omnimarket` | Optional |
| `pr_number` | Pull request number to polish | Optional |
| `ticket_id` | Linear ticket ID for traceability | Optional |
| `required_clean_runs` | Consecutive clean local-review passes required before done | `4` |
| `max_iterations` | Maximum local-review cycles before stopping | `10` |
| `skip_conflicts` | Skip merge conflict resolution | `false` |
| `skip_pr_review` | Skip PR review comments and CI repair | `false` |
| `skip_local_review` | Skip local review | `false` |
| `no_ci` | Skip CI fetch in the PR review phase | `false` |
| `no_push` | Apply fixes locally without pushing | `false` |
| `no_automerge` | Skip enabling GitHub automerge at the end | `false` |
| `dry_run` | Run without side effects | `false` |
| `worktree_path` | Explicit worktree path override for live polish | Optional |
| `run_dir` | Explicit state directory for breadcrumbs and `result.json` | Optional |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "pr_polish" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `repo`
- `pr_number`
- `ticket_id`
- `required_clean_runs`
- `max_iterations`
- `skip_conflicts`
- `skip_pr_review`
- `skip_local_review`
- `no_ci`
- `no_push`
- `no_automerge`
- `dry_run`
- `worktree_path`
- `run_dir`

For live polish, include both `repo` and `pr_number`. For proof or planning
runs, set `dry_run: true` or use `--compile-only` when the user only wants to
validate dispatch shape.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_pr_polish/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `pr_polish`
- Runtime topic: `onex.cmd.omnimarket.pr-polish-start.v1`
- Completion topic: `onex.evt.omnimarket.pr-polish-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `final_phase`, `conflicts_resolved`,
`ci_fixes_applied`, `comments_addressed`, `error_message`, and any state or
run artifact paths emitted by the node. All readiness decisions come from
`node_pr_polish`.
