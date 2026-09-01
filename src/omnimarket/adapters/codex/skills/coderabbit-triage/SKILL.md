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

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `coderabbit_triage`
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

- **Backing node:** `node_coderabbit_triage`
- **Contract command name:** `coderabbit_triage`
- **Contract command topic:** `onex.cmd.omnimarket.coderabbit-triage-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.coderabbit-triage-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


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
- Target contract command topic: `onex.cmd.omnimarket.coderabbit-triage-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.coderabbit-triage-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `total_threads`, `blocking_count`,
`suggestion_count`, `unknown_count`, `resolved_count`, and a concise thread
summary with severity, matched keyword, and URL when present. For `dry_run`,
make clear that no replies or thread resolution were performed.
