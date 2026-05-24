# OMN-11925 Reasoning Model Upgrade Note

Captured: 2026-05-24

## Why this patch exists

OMN-11891 post-merge runtime proof showed the delegation path is live, but the selected local reasoning model timed out consistently:

- Selected model: `Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ`
- Endpoint role: `local-deepseek-r1-14b`
- Observed result: terminal failure events and projection rows were durable, but success-path proof was blocked by `TimeoutError`.

## Change

This patch updates the OMN-11925 registry/topology reconciliation so new reasoning/research routing prefers the newer Qwen3.6 local slot:

- Adds canonical registry key `qwen3.6-35b`.
- Binds it to `LLM_QWEN3_NEXT_URL` and served model name `mlx-community/Qwen3.6-35B-A3B-8bit`.
- Adds Bifrost backend `local-qwen3-6-35b`.
- Updates research routing to prefer `local-qwen3-6-35b`, with `local-deepseek-r1-14b` retained as compatibility fallback.
- Updates task-class overrides for reasoning, complex_reasoning, planning, review, and research to `qwen3.6-35b`.

## Live topology caveat

Read-only probes from this session confirmed `.201` currently serves only:

- `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` on `:8000`
- `Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ` on `:8001`

SSH to `.200` was not available from this session, so `.200:8102` serving was not independently re-probed here. Runtime success-path proof still requires the stability overlay/runtime to resolve `local-qwen3-6-35b` to the newer served endpoint.

## Verification

```bash
uv run pytest tests/nodes/node_delegation_routing_reducer/test_bifrost_overlay_loader.py \
  tests/unit/models/delegation/llm_cost_routing/test_model_registry.py -q
```

Result: `23 passed in 0.20s`.
