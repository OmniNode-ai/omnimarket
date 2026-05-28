---
name: observability-sink
description: Thin Codex skill shim for node_observability_sink_effect. Use when persisting OmniNode observability events through runtime adapters.
---

# Observability Sink

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_observability_sink_effect` node. The node owns observability persistence
through typed runtime adapters. Do not add ActionLogger calls, direct database
writes, direct Kafka producers, handler imports, or local storage fallbacks to
this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `correlation_id` | UUID correlation id for the run | Required |
| `session_id` | UUID session id | Required |
| `events` | Ordered action event payloads | Required |
| `sink_kafka` | Persist through the injected Kafka sink adapter | `true` |
| `sink_postgres` | Persist through the injected PostgreSQL sink adapter | `true` |
| `submitted_at` | ISO-8601 submission timestamp | Current UTC time |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "observability_sink_effect" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

For a side-effect-free runtime smoke, set both `sink_kafka` and `sink_postgres`
to `false`. Do not replace that with direct file, database, or broker writes.

## Contract

- Backing node: `src/omnimarket/nodes/node_observability_sink_effect/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `observability_sink_effect`
- Runtime topic: `onex.cmd.omnimarket.observability-sink.v1`
- Completion topic: `onex.evt.omnimarket.observability-persisted.v1`
