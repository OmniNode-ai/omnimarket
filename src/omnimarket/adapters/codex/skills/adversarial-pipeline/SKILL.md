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

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "adversarial_pipeline_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into `topic`, `plan_path`, `min_findings_gate`,
`linear_project`, `no_launch`, and `dry_run`. Surface `error.code` and
`error.message` directly; do not call legacy agent or Linear paths when the node
returns an explicit not-implemented stop state.

## Contract

- Backing node: `src/omnimarket/nodes/node_adversarial_pipeline_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `adversarial_pipeline_orchestrator`
- Runtime topic: `onex.cmd.omnimarket.adversarial-pipeline-start.v1`
- Completion topic: `onex.evt.omnimarket.adversarial-pipeline-completed.v1`
