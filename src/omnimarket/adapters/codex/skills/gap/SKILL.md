---
name: gap
description: Thin Codex skill shim for node_gap_compute. Use when running OmniNode integration gap detection, report classification, or gap-cycle preflight.
---

# Gap

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_gap_compute` node. The node owns contract/report gap detection and
classification. Do not add Linear, GitHub, grep, or fix orchestration logic to
this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `subcommand` | `detect`, `fix`, `cycle`, or `reconcile` | `detect` |
| `scope` | Operator-facing gap scope | `local` |
| `epic` | Optional epic identifier | Omitted |
| `report` | Existing gap report path for fix/reconcile | Omitted |
| `repo` | Limit detect to one repo name | Omitted |
| `repo_roots` | Repo roots to scan | Node default |
| `since_days` | Closed-epic lookback hint | `30` |
| `severity_threshold` | `WARNING` or `CRITICAL` | `WARNING` |
| `max_findings` | Maximum deterministic findings | `200` |
| `max_best_effort` | Maximum best-effort findings | `50` |
| `max_iterations` | Cycle iteration cap | `3` |
| `output` | `json` or `md` | `json` |
| `dry_run` | Run without side effects | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "gap_compute" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `subcommand`
- `scope`
- `epic`
- `report`
- `repo`
- `repo_roots`
- `since_days`
- `severity_threshold`
- `max_findings`
- `max_best_effort`
- `max_iterations`
- `output`
- `dry_run`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_gap_compute/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `gap_compute`
- Runtime topic: `onex.cmd.omnimarket.gap-compute.v1`
- Completion topic: `onex.evt.omnimarket.gap-compute.v1`

## Output

Prefer `output_payloads[0]`. Render `status`, scanned contract count,
findings grouped by category and severity, skipped probes, and report
classification counts when present. All gap behavior is owned by
`node_gap_compute`.
