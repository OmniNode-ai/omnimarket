---
name: dep-cascade-dedup
description: Thin Codex skill shim for node_dep_cascade_dedup_orchestrator. Use when deduplicating dependency bump PR cascades through runtime truth.
---

# Dep Cascade Dedup

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_dep_cascade_dedup_orchestrator` node. The node owns PR discovery,
version grouping, close decisions, and GitHub mutations. Do not add GitHub CLI
calls, GitHub API calls, handler imports, or local dedup logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `dep_cascade_dedup_orchestrator`
- **Request wrapper:** `scripts/run_codex_runtime_request.py`
- **Route:** generic Pattern-B adapter transport.
- **Adapter transport command topic:** `onex.cmd.omnimarket.pattern-b-dispatch.v1`
- **Adapter transport response topic:** `onex.evt.omnimarket.pattern-b-dispatch-completed.v1`
- **Compile-only:** pass `--compile-only` to validate the request and binding
  without publishing an event or starting a runtime. This is adapter preflight,
  not evidence that a target runtime executed the command.
- **Runtime evidence:** inspect `runtime_evidence.runtime_observation` and
  `runtime_evidence.adapter_dispatch_binding`; compile-only is `UNOBSERVED`
  with reason `compile_only`.
- **Evidence wire schema:** `runtime_evidence.schema_version` is
  `runtime-evidence/v2`; v2 requires `runtime_observation` and carries the
  resolved node contract under `adapter_dispatch_binding.node_contract`.
- **Binding fields:** `adapter_dispatch_binding` reports
  `adapter_command_topic`, `requested_response_topic`,
  `selected_terminal_topic`, and `terminal_selection` (`NODE_CONTRACT`,
  `DIRECT_DELEGATE_SKILL_CONTRACT`, or `EXPLICIT_RESPONSE_OVERRIDE`); its
  `node_contract` is the resolved, typed contract binding.

### Target node contract metadata

- **Backing node:** `node_dep_cascade_dedup_orchestrator`
- **Contract command name:** `node_dep_cascade_dedup_orchestrator`
- **Contract command topic:** `onex.cmd.omnimarket.dep-cascade-dedup-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.dep-cascade-dedup-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `repos` | Repo slugs to scan | Node default |
| `dependency_type` | Optional dependency type filter | `""` |
| `label` | Dependency PR label | `dependencies` |
| `dry_run` | Report superseded PRs without closing | `false` |
| `close_comment` | Optional close comment override | Node default |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "dep_cascade_dedup_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 600000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `repos`
- `dependency_type`
- `label`
- `dry_run`
- `close_comment`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly. A
`node_not_implemented` response is the current explicit stop state, not
permission to run direct GitHub commands from this skill.

## Contract

- Backing node: `src/omnimarket/nodes/node_dep_cascade_dedup_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `dep_cascade_dedup_orchestrator`
- Target contract command topic: `onex.cmd.omnimarket.dep-cascade-dedup-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.dep-cascade-dedup-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `repos_scanned`, `groups_found`,
`prs_closed`, `prs_kept`, `prs_skipped`, and package-group summaries. All
dedup behavior is owned by `node_dep_cascade_dedup_orchestrator`.
