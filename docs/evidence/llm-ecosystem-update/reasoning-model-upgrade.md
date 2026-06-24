# Reasoning Model Upgrade Note

Captured: 2026-05-24

## Why this patch exists

A post-merge runtime proof showed the delegation path is live, but the selected local reasoning model timed out consistently:

- Selected model: legacy DeepSeek 14B local slot
- Endpoint role: `local-deepseek-r1-14b`
- Observed result: terminal failure events and projection rows were durable, but success-path proof was blocked by `TimeoutError`.

## Change

This patch updates the registry/topology reconciliation so new reasoning/research routing prefers the newer Qwen3.6 local slot:

- Adds canonical registry key `qwen3.6-35b`.
- Binds it to `LLM_QWEN3_NEXT_URL` and served model name `mlx-community/Qwen3.6-35B-A3B-8bit`.
- Adds Bifrost backend `local-qwen3-6-35b`.
- Updates research routing to prefer `local-qwen3-6-35b`, with `local-deepseek-r1-14b` retained as compatibility fallback.
- Updates task-class overrides for reasoning, complex_reasoning, planning, review, and research to `qwen3.6-35b`.

## Live topology caveat

Read-only probes from this session confirmed the runtime host currently serves only:

- legacy Qwen3-Coder local slot on `:8000`
- legacy DeepSeek 14B local slot on `:8001`

The secondary inference host was not available from this session, so its `:8102` serving was not independently re-probed here. Runtime success-path proof still requires the stability overlay/runtime to resolve `local-qwen3-6-35b` to the newer served endpoint.

## Verification

```bash
uv run pytest tests/nodes/node_delegation_routing_reducer/test_bifrost_overlay_loader.py \
  tests/unit/models/delegation/llm_cost_routing/test_model_registry.py -q
```

Result: `23 passed in 0.20s`.
