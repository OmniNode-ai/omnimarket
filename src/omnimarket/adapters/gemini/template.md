# {{SKILL_DISPLAY_NAME}} — Gemini CLI Instructions

You have access to the OmniMarket `{{NODE_NAME}}` node via the ONEX event bus.
When the user asks you to {{TRIGGER_DESCRIPTION}}, use this procedure. **Do not
implement the logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| {{ARG_1}} | {{ARG_1_DESCRIPTION}} | {{ARG_1_DEFAULT}} |
| {{ARG_2}} | {{ARG_2_DESCRIPTION}} | {{ARG_2_DEFAULT}} |

## Procedure

### Step 1 — Assemble payload

Build a JSON payload from the user's request:

```json
{
  "correlation_id": "<generate a UUID v4>",
  "{{PAYLOAD_FIELD_1}}": "{{EXAMPLE_VALUE_1}}",
  "{{PAYLOAD_FIELD_2}}": "{{EXAMPLE_VALUE_2}}"
}
```

Only include fields the user explicitly specified. The node applies defaults for
omitted fields.

### Step 2 — Publish command event

Publish to the ONEX event bus:
- **Topic:** `{{COMMAND_TOPIC}}`
- **Payload:** The JSON from Step 1

Source: `contract.yaml → event_bus.subscribe_topics[0]`

### Step 3 — Monitor completion

Listen on the ONEX event bus:
- **Topic:** `{{COMPLETION_TOPIC}}`
- **Filter:** Match the `correlation_id` from Step 1
- **Timeout:** {{TIMEOUT_MS}} ms

Source: `contract.yaml → event_bus.publish_topics[0]` and `descriptor.timeout_ms`

### Step 4 — Format output

On success: render the completion payload in a clear format for the user.

On timeout: report that the operation timed out after {{TIMEOUT_MS}} ms.

On error: surface the error message from the completion event payload.

## Important

Do not implement any business logic. All processing runs in the OmniMarket
`{{NODE_NAME}}` node. These instructions only cover event publish/subscribe and
output formatting.
