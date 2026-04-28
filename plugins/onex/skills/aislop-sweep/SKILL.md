---
name: aislop-sweep
description: Thin Codex skill shim for node_aislop_sweep. Use when scanning OmniNode repos for AI-generated quality anti-patterns or AI slop.
---

# AI Slop Sweep

This skill is a thin Pattern B broker shim over the OmniMarket
`node_aislop_sweep` node. Collect arguments, dispatch the node, and render the
node result. Do not add scan logic, grep fallbacks, ticket logic, or
remediation logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `target_dirs` | Absolute repo paths to scan | Required |
| `checks` | Optional list of check categories | All checks |
| `--dry-run` | Report findings without side effects | `false` |
| `severity_threshold` | Minimum severity to report | `WARNING` |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run scripts/run_codex_runtime_request.py \
  --command-name "aislop_sweep" \
  --payload '<json-payload>' \
  --timeout-ms 120000
```

Map user inputs into a JSON payload:

- `target_dirs` -> absolute path list in `target_dirs`
- `checks` -> category list in `checks`
- `dry_run=true` -> `dry_run: true`
- `severity_threshold` -> `severity_threshold`

If the user supplies repo slugs instead of absolute paths, resolve them under
`$OMNI_HOME` before dispatch and place the resulting absolute paths in
`target_dirs`.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_aislop_sweep/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `aislop_sweep`
- Runtime topic: `onex.cmd.omnimarket.aislop-sweep-start.v1`
- Completion topic: `onex.evt.omnimarket.aislop-sweep-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the findings summary grouped by severity
and repo, with counts and file references when present. All finding detection
is owned by `node_aislop_sweep`.
