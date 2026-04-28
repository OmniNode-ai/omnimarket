---
name: merge-sweep
description: Thin Codex skill shim for node_pr_lifecycle_orchestrator. Use for org-wide PR inventory, triage, merge, or fix sweeps.
---

# Merge Sweep

This skill is a thin Pattern B broker shim over the OmniMarket
`node_pr_lifecycle_orchestrator` node. The node owns PR inventory, triage,
verification, merge, and fix dispatch behavior. Do not add GitHub scripting,
queue logic, or PR classification logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `repos` | Comma-separated repo slugs to filter | `""` |
| `dry_run` | Run without side effects | `false` |
| `inventory_only` | Stop after PR inventory | `false` |
| `fix_only` | Only run the fix phase | `false` |
| `merge_only` | Only run the merge phase | `false` |
| `enable_auto_rebase` | Auto-rebase stale PR branches before merge | `true` |
| `verify` | Run verification before merge | `false` |
| `verify_timeout_seconds` | Per-PR verification timeout | `30` |
| `onex_state_dir` | Optional state artifact directory override | Default `ONEX_STATE_DIR` |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run scripts/run_codex_runtime_request.py \
  --command-name "pr_lifecycle_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

Map user inputs into a JSON payload using the same field names:

- `correlation_id`
- `run_id`
- `repos`
- `onex_state_dir`
- `dry_run`
- `inventory_only`
- `fix_only`
- `merge_only`
- `enable_auto_rebase`
- `verify`
- `verify_timeout_seconds`

Always include:

- `correlation_id`: UUIDv4, generated if the user does not supply one
- `run_id`: filesystem-safe identifier such as `merge-sweep-YYYYMMDDTHHMMSSZ`

Only include `onex_state_dir` when the user explicitly wants a non-default
artifact location.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_pr_lifecycle_orchestrator/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `pr_lifecycle_orchestrator`
- Runtime topic: `onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`
- Completion topic: `onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the node result with PR counts and phase
outcomes: `prs_inventoried`, `prs_merged`, `prs_fixed`, and `prs_skipped`. All
decisions come from `node_pr_lifecycle_orchestrator`.
