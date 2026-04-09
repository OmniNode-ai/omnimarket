# AI Slop Sweep — Gemini CLI Instructions

You have access to the OmniMarket aislop-sweep node via the ONEX event bus. When
the user asks you to scan for AI-generated quality anti-patterns, code quality
issues, or "AI slop", use this procedure. **Do not implement scanning logic
yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| repos | Comma-separated repo names to scan | All supported repos |
| checks | Pattern categories: phantom-callables, compat-shims, prohibited-patterns, hardcoded-topics, todo-fixme, todo-stale, empty-impls | All |
| dry_run | Scan and report only — no tickets, no fixes | false |
| ticket | Create Linear tickets for findings | false |
| auto_fix | Attempt auto-fix for trivial patterns | false |
| severity_threshold | Minimum severity: WARNING or ERROR | WARNING |

## Procedure

### Step 1 — Assemble payload

Build a JSON payload from the user's request:

```json
{
  "correlation_id": "<generate a UUID v4>",
  "repos": ["omniclaude", "omnibase_core"],
  "checks": ["phantom-callables", "todo-fixme"],
  "dry_run": true,
  "ticket": false,
  "auto_fix": false,
  "severity_threshold": "WARNING"
}
```

Only include fields the user explicitly specified. The node applies defaults for
omitted fields.

### Step 2 — Publish command event

Publish to the ONEX event bus:
- **Topic:** `onex.cmd.market.aislop-sweep-requested.v1`
- **Payload:** The JSON from Step 1

### Step 3 — Monitor completion

Listen on the ONEX event bus:
- **Topic:** `onex.evt.market.aislop-sweep-completed.v1`
- **Filter:** Match the `correlation_id` from Step 1
- **Timeout:** 120000 ms (2 minutes)

### Step 4 — Format output

On success: render the completion payload as a summary with findings grouped by
severity and check category. Include total counts and per-repo breakdowns.

On timeout: report that the sweep timed out after 120 seconds.

On error: surface the error message from the completion event payload.

## Important

Do not implement any scanning, triaging, or ticket-creation logic. All business
logic runs in the OmniMarket `aislop_sweep` node. These instructions only cover
event publish/subscribe and output formatting.
