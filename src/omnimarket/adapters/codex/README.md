# Codex Adapter — OmniMarket

## Overview

Codex adapters are instruction files that tell the OpenAI Codex agent how to
invoke OmniMarket packages through the runtime-owned local ingress. Each
instruction file describes the request/response wrapper for a single node.
**No business logic lives in the instruction file.**

## How it works

1. The Codex agent reads the instruction file as part of its system context
2. When the user requests the relevant operation, the agent builds a JSON payload
3. The agent invokes `scripts/run_codex_runtime_request.py` against the local runtime socket
4. The runtime dispatches the backing OmniMarket node and returns a typed JSON response
5. The agent formats `dispatch_result` or surfaces the structured runtime error

## File conventions

| File | Purpose |
|------|---------|
| `aislop-sweep-instructions.md` | Example instructions for the aislop-sweep node |
| `pr-lifecycle-orchestrator-instructions.md` | Example instructions for the PR lifecycle / merge-sweep node |
| `session-bootstrap-instructions.md` | Example instructions for the session bootstrap node |
| `session-orchestrator-instructions.md` | Example instructions for the session orchestrator node |
| `template.md` | Generic template with placeholders for new instructions |
| `runtime_client.py` | Stdlib-friendly local runtime ingress client |
| `scripts/run_codex_runtime_request.py` | Repo-local request wrapper that bootstraps `src/` import resolution |

Instructions are provided to Codex via its instruction/system prompt configuration.

## Wrapper responsibilities

1. **Argument collection and validation** — Parse user-provided arguments and map
   them to the event payload schema from `contract.yaml`.
2. **Command options mapping** — Translate arguments into the structured event
   payload fields expected by the node.
3. **Correlation ID generation** — Generate a unique `correlation_id` (UUID v4) for
   each invocation to track request/response pairs inside the runtime dispatch path.
4. **Runtime ingress dispatch** — Invoke the local runtime client instead of
   publishing directly to Kafka or calling the node CLI.
5. **Response handling** — Parse the runtime JSON response and distinguish
   `dispatch_result` from structured `error`.
6. **Output formatting** — Transform the runtime response into a clear reply
   for the user.
7. **Timeout and error handling** — Pass the node timeout to the runtime ingress,
   report structured runtime errors clearly, and treat dry-run GitHub/Linear
   reachability gaps as explicit degraded states.

## Creating new instructions

1. Copy `template.md` to `<skill-name>-instructions.md`
2. Replace all `{{PLACEHOLDER}}` values using the node's `contract.yaml`
3. Add the resulting file to your Codex agent's instruction configuration
