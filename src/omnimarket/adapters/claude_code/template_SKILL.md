---
description: "{{SKILL_DESCRIPTION}}"
version: 1.0.0
mode: full
level: advanced
debug: false
category: "{{CATEGORY}}"
tags:
  - omnimarket
  - "{{TAG_1}}"
  - "{{TAG_2}}"
author: OmniMarket
composable: true
args:
  # Copy args from the node's contract.yaml and map to CLI flags
  - name: --example-flag
    description: "Example argument — replace with actual node arguments"
    required: false
inputs:
  - name: "{{INPUT_NAME}}"
    description: "{{INPUT_DESCRIPTION}}"
outputs:
  - name: skill_result
    description: "Completion event payload from the OmniMarket node"
---

# {{SKILL_DISPLAY_NAME}} (OmniMarket)

## Overview

Thin event-bus wrapper around the OmniMarket `{{NODE_NAME}}` node. This skill
publishes a command event and monitors for completion — all business logic
executes in the node handler.

**Announce at start:** "Running {{SKILL_SLUG}} via OmniMarket event bus."

## Execution

### Step 1 — Assemble payload

Collect arguments from the user invocation and build the command payload:

```json
{
  "correlation_id": "<uuid4>",
  "{{PAYLOAD_FIELD_1}}": "{{EXAMPLE_VALUE_1}}",
  "{{PAYLOAD_FIELD_2}}": "{{EXAMPLE_VALUE_2}}"
}
```

Omit fields the user did not specify — the node applies its own defaults.

### Step 2 — Publish command event

Publish to topic: `{{COMMAND_TOPIC}}`

Source: `contract.yaml → event_bus.subscribe_topics[0]`

### Step 3 — Monitor completion

Listen on topic: `{{COMPLETION_TOPIC}}`

Source: `contract.yaml → event_bus.publish_topics[0]`

Filter by `correlation_id`. Timeout: **{{TIMEOUT_MS}} ms** (from contract `descriptor.timeout_ms`).

### Step 4 — Format output

On success, render the completion payload in a format appropriate for the skill's
output type. On timeout or error, report the failure clearly.

## CLI

```
/{{SKILL_SLUG}}                    # Default invocation
/{{SKILL_SLUG}} --example-flag     # With options
```

## Important

This wrapper contains **no business logic**. Do not add domain logic here.
All processing is handled by the `{{NODE_NAME}}` node in
`omnimarket/nodes/{{NODE_DIR}}/`.

Do not add concrete LLM provider names, served model IDs, endpoint URLs, or
fallback model defaults to this skill. If the backing node requires LLM work,
pass logical routing needs only when those fields are declared in the node
contract; runtime model resolution belongs to `node_model_router` and
contract/overlay policy.
