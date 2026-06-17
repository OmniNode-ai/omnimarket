# Delegation Dispatch Architecture

OmniMarket's delegation path routes a caller's prompt to the cheapest capable
backend and escalates automatically when quality gates fail.

## Dispatch path (canonical as of OMN-13160)

```
Caller (omniclaude skill / Codex adapter)
  │
  ▼
node_delegate_skill_orchestrator  (effect handler, contract.yaml)
  │  publishes: onex.cmd.omnimarket.delegate-skill.v1
  │
  ▼
node_delegation_orchestrator  (orchestrator, owned by omnimarket)
  │  route: onex.cmd.omnibase-infra.delegation-request.v1
  │
  ▼
node_llm_delegation_call_effect  (effect handler)
  │  dispatches via: DirectCurl posts endpoint_url VERBATIM (OMN-13159)
  │
  ▼
Backend (local vLLM / cloud API)
  │
  ▼
node_delegation_quality_gate_reducer  (reducer, FSM transition)
  │  on pass: onex.evt.omnibase-infra.delegation-completed.v1
  │  on fail: triggers escalation emit (OMN-13140)
  │
  ▼
node_delegate_skill_orchestrator  collects terminal event and returns result
```

Before OMN-13160, the dispatch path included bespoke port objects
(`source_tool: delegate-skill-runtime-port`) that owned HTTP client lifecycle
outside the canonical handler boundary. OMN-13160 deleted those ports; the
canonical effect handler now owns the full dispatch.

## endpoint_url verbatim rule (OMN-13159)

Every backend in `src/omnimarket/configs/bifrost_delegation.yaml` carries a
`endpoint_url` that is the **complete, final URL** including the full chat path
(e.g. `https://api.anthropic.com/v1/chat/completions`). The call site posts
this value verbatim — no in-code construction, append, rstrip, or
path-resolver exists. A bare base URL (no chat path) is a misconfiguration and
the resolver fails closed.

For site-local backends the `endpoint_url` is `null` in the repo default; the
`endpoint_url_env` key names the environment variable (or overlay file key)
that must hold the **complete** URL. The overlay file is typically
`~/.omninode/delegation/bifrost_overrides.yaml`.

## Escalation gate sequence (OMN-13140 / OMN-13143)

When a quality gate fails the orchestrator emits a
`onex.evt.omnimarket.delegation-escalation-requested.v1` event. The escalation
path tries backends in the order defined by `routing_tiers.yaml`, cheapest
first:

1. `local` tier — vLLM models on the local inference server
2. `cheap_cloud` tier — cost-effective hosted APIs (GLM-4.5, Gemini Flash)
3. `frontier_api` tier — Claude Sonnet, Claude Haiku, Gemini Pro

Each tier is attempted at most once. If all tiers are exhausted without a
passing quality gate the orchestrator emits
`onex.evt.omnimarket.delegate-skill-failed.v1`.

OMN-13143 wired the escalation emit publisher on the dispatch path so that
escalation events are visible to downstream projections
(`node_llm_delegation_projection`) even when the final attempt succeeds.

## per-backend max_tokens (OMN-13161)

Each backend entry in `bifrost_delegation.yaml` carries a `max_tokens` field
that caps the output-token budget for that backend. When the caller omits
`max_tokens` from the delegation request the orchestrator resolves the
effective value from the selected backend's ceiling. An explicit caller value
is capped at that ceiling. The contract-level `maximum: 200000` field is the
absolute schema bound; the per-backend ceiling is typically lower.

See `bifrost_delegation.yaml` for current per-backend values.

## Related nodes

| Node | Archetype | Role |
| --- | --- | --- |
| `node_delegate_skill_orchestrator` | Orchestrator | Consumer-facing entry point |
| `node_delegation_orchestrator` | Orchestrator | Internal dispatch coordinator |
| `node_delegation_routing_reducer` | Reducer | Selects backend from routing tiers |
| `node_delegation_quality_gate_reducer` | Reducer | Evaluates result against criteria |
| `node_llm_delegation_call_effect` | Effect | Posts request to backend endpoint |
| `node_delegation_ab_runner` | Compute | A/B routing experiment runner |
| `node_llm_delegation_projection` | Projection | Materializes delegation event stream |

## Related configuration

- `src/omnimarket/configs/bifrost_delegation.yaml` — backend definitions, per-backend max_tokens
- `src/omnimarket/configs/routing_tiers.yaml` — tier escalation ladder
- `~/.omninode/delegation/bifrost_overrides.yaml` — local endpoint overlay (not committed)
