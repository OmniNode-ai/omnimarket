---
name: {{SKILL_SLUG}}
description: Thin Codex skill shim for the OmniMarket {{NODE_NAME}} node. Use when the user asks to {{TRIGGER_DESCRIPTION}}.
---

# {{SKILL_DISPLAY_NAME}}

You have access to the OmniMarket `{{NODE_NAME}}` node through the Pattern B
broker client. When the user asks you to {{TRIGGER_DESCRIPTION}}, use this
procedure. Do not implement the node logic yourself.

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
{{ARGS_TABLE}}

## Procedure

### Step 1 - Build JSON payload

Map user-provided arguments into a JSON object that matches the backing node's
input model. Omit fields the user did not specify so the node can apply its
own defaults.

Use this dispatch shape:

```json
{{PAYLOAD_TEMPLATE}}
```

### Step 2 - Dispatch through the Pattern B broker client

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH /opt/homebrew/bin/python3.13 scripts/run_codex_runtime_request.py \
  --command-name "{{NODE_ALIAS}}" \
  --payload '<json-payload>' \
  --timeout-ms {{TIMEOUT_MS}}
```

The command prints a JSON response object to stdout.

### Step 3 - Interpret the response

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result and render that clearly for the user.

If `ok` is `true` and `output_payloads` is absent, fall back to rendering
`dispatch_result`.

If `ok` is `false`, surface `error.code` and `error.message` directly.

If a dry run depends on GitHub or Linear and those systems are unreachable,
report that degraded condition explicitly rather than inventing remote state.

### Step 4 - Format output

On success: prefer `output_payloads[0]`; if it is absent, render the runtime
`dispatch_result`.

On timeout: report that the operation timed out.

On error: surface the broker client error code and message.

## Contract

- Backing node: `omnimarket/nodes/{{NODE_DIR}}/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `{{NODE_ALIAS}}`
- Command topic: `{{COMMAND_TOPIC}}`
- Completion topic: `{{COMPLETION_TOPIC}}`
- Contract timeout: {{TIMEOUT_MS}} ms

## Important

Do not implement any business logic. All processing runs in the OmniMarket
`{{NODE_NAME}}` node. These instructions only cover argument mapping, node
dispatch, and output formatting.
