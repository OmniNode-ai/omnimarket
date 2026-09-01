---
name: adversarial-pipeline
description: Thin Codex skill shim for node_adversarial_pipeline_orchestrator. Use when running the adversarial plan-to-ticket pipeline through runtime truth.
---

# Adversarial Pipeline

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_adversarial_pipeline_orchestrator` node. The node owns design-to-plan,
hostile-review gating, and ticket creation orchestration. Do not add background
agent CLI calls, Linear API calls, handler imports, or local pipeline logic to
this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `adversarial_pipeline_orchestrator`
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

- **Backing node:** `node_adversarial_pipeline_orchestrator`
- **Contract command name:** `node_adversarial_pipeline_orchestrator`
- **Contract command topic:** `onex.cmd.omnimarket.adversarial-pipeline-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.adversarial-pipeline-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `topic` | Design topic or problem statement | Required |
| `plan_path` | Existing plan path; skips design stage when set | Omitted |
| `min_findings_gate` | Minimum hostile-review findings required | `3` |
| `linear_project` | Optional Linear project for tickets | Omitted |
| `no_launch` | Skip browser launch in design stage | `false` |
| `dry_run` | Run without creating tickets | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "adversarial_pipeline_orchestrator" \
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

- `topic`
- `plan_path`
- `min_findings_gate`
- `linear_project`
- `no_launch`
- `dry_run`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly. A
`node_not_implemented` response is the current explicit stop state, not
permission to call legacy agent or Linear paths from this skill.

## Contract

- Backing node: `src/omnimarket/nodes/node_adversarial_pipeline_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `adversarial_pipeline_orchestrator`
- Target contract command topic: `onex.cmd.omnimarket.adversarial-pipeline-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.adversarial-pipeline-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `stage_reached`, `gate_passed`,
`findings_count`, `tickets_created`, and any created ticket identifiers. All
pipeline behavior is owned by `node_adversarial_pipeline_orchestrator`.
