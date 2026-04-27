---
name: session-bootstrap
description: Thin Codex skill shim for node_session_bootstrap. Use to initialize a session contract snapshot and launchd scheduler plan.
---

# Session Bootstrap

This skill is a thin runtime-ingress shim over the OmniMarket
`node_session_bootstrap` node. The node owns session contract validation,
snapshot persistence, and scheduler-plan emission. Do not add timer setup
logic or fallback scheduler logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `session_id` | UUID for the session run | Required |
| `session_label` | Human-readable session label | `<today> session` |
| `phases_expected` | Comma-separated expected phases | `build_loop,merge_sweep,platform_readiness` |
| `max_cycles` | Maximum build loop cycles (`0` = unlimited) | `0` |
| `cost_ceiling` | Advisory cost ceiling in USD | `10.0` |
| `session_mode` | Session mode: `build`, `close-out`, or `reporting` | `build` |
| `active_sprint_id` | Explicit Linear cycle id or `auto-detect` | `auto-detect` |
| `model_routing_preference` | `local-first`, `frontier-only`, or `hybrid` | `local-first` |
| `state_dir` | State output directory | `.onex_state` |
| `dry_run` | Build artifacts without mutating scheduler state | `false` |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH /opt/homebrew/bin/python3.13 scripts/run_codex_runtime_request.py \
  --node-alias "session_bootstrap" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

Map user inputs into a JSON payload using the same field names:

- `session_id`
- `session_label`
- `phases_expected`
- `max_cycles`
- `cost_ceiling`
- `session_mode`
- `active_sprint_id`
- `model_routing_preference`
- `state_dir`
- `dry_run`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_session_bootstrap/`
- Local request wrapper: `scripts/run_codex_runtime_request.py`
- Route alias: `session_bootstrap`
- Runtime topic: `onex.cmd.omnimarket.session-bootstrap-start.v2`
- Completion topic: `onex.evt.omnimarket.session-bootstrap-completed.v2`

## Output

Prefer `output_payloads[0]`. Render the node result with `status`,
`contract_path`, `crons_registered`, and `warnings`. Treat an empty
`crons_registered` list as valid when the node emits the launchd scheduler plan
without activating any cron shim.
