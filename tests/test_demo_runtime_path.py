"""Runtime-local proof for the OMN-9359 demo market path."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.adapters.codex.runtime_client import CodexRuntimeRequestAdapter


async def _dispatch_local(
    *,
    command_name: str,
    payload: dict[str, object],
    response_topic: str,
) -> dict[str, object]:
    client = CodexRuntimeRequestAdapter(requester="demo-runtime-test")
    result = await client.dispatch_async(
        command_name=command_name,
        payload=payload,
        timeout_ms=120_000,
        response_topic=response_topic,
        runtime_selection="local",
    )
    assert result.ok is True
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.event_bus_backend == "inmemory"
    return result.output_payloads[0]


@pytest.mark.asyncio
async def test_demo_delegation_dry_run_flows_through_native_nodes() -> None:
    run_id = uuid4()
    correlation_id = uuid4()
    tasks = [
        "Write a pytest test for a function that adds two integers",
        "Which is cheaper: GPT-4o or Gemini Flash 2.0 for summarization tasks?",
        "Run the ONEX skill router for ticket triage",
    ]
    model_configs = [
        {
            "model_id": "gemini/gemini-2.0-flash",
            "endpoint_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "provider": "openai_compatible",
            "api_key_env_var": "GEMINI_API_KEY",
        },
        {
            "model_id": "claude-opus-4-5",
            "endpoint_url": "claude-cli://local",
            "provider": "claude_cli",
        },
        {
            "model_id": "claude-sonnet-4-6",
            "endpoint_url": "claude-cli://local",
            "provider": "claude_cli",
        },
        {
            "model_id": "onex-deterministic",
            "endpoint_url": "fixture://onex-deterministic",
            "provider": "deterministic_fixture",
        },
    ]
    provider_fixtures = {
        "gemini/gemini-2.0-flash": {
            "outputs": ["gemini fixture"] * len(tasks),
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "latency_ms": 11.0,
        },
        "claude-opus-4-5": {
            "outputs": ["opus fixture"] * len(tasks),
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "latency_ms": 14.0,
        },
        "claude-sonnet-4-6": {
            "outputs": ["sonnet fixture"] * len(tasks),
            "prompt_tokens": 45,
            "completion_tokens": 22,
            "latency_ms": 13.0,
        },
        "onex-deterministic": {
            "outputs": ["deterministic fixture"] * len(tasks),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latency_ms": 0.0,
        },
    }

    fanout_payload = await _dispatch_local(
        command_name="demo_fanout_orchestrator",
        payload={
            "run_id": str(run_id),
            "correlation_id": str(correlation_id),
            "tasks": tasks,
            "model_configs": model_configs,
            "dry_run": True,
            "provider_fixtures": provider_fixtures,
        },
        response_topic="onex.evt.omnibase-infra.demo-fanout-runtime-test.v1",
    )

    assert len(fanout_payload["results"]) == len(tasks) * len(model_configs)
    assert {
        item["model_id"]
        for item in fanout_payload["results"]  # type: ignore[index]
    } == {item["model_id"] for item in model_configs}

    cost_payload = await _dispatch_local(
        command_name="demo_cost_compute",
        payload={
            "inference_results": fanout_payload["results"],
            "pricing_table": {
                "gemini/gemini-2.0-flash": {
                    "prompt_cost_per_1k": 0.000075,
                    "completion_cost_per_1k": 0.0003,
                },
                "claude-opus-4-5": {
                    "prompt_cost_per_1k": 0.015,
                    "completion_cost_per_1k": 0.075,
                },
                "claude-sonnet-4-6": {
                    "prompt_cost_per_1k": 0.003,
                    "completion_cost_per_1k": 0.015,
                },
                "onex-deterministic": {
                    "prompt_cost_per_1k": 0.0,
                    "completion_cost_per_1k": 0.0,
                },
            },
        },
        response_topic="onex.evt.omnibase-infra.demo-cost-runtime-test.v1",
    )

    assert cost_payload["cheapest_model_id"] == "onex-deterministic"
    assert len(cost_payload["costs"]) == len(model_configs)

    render_payload = await _dispatch_local(
        command_name="demo_renderer_effect",
        payload={
            "cost_result": {
                "costs": cost_payload["costs"],
                "cheapest_model_id": cost_payload["cheapest_model_id"],
            },
            "bar_width": 24,
            "title": "OMN-9359 Demo Cost Comparison",
        },
        response_topic="onex.evt.omnibase-infra.demo-render-runtime-test.v1",
    )

    chart_lines = render_payload["chart_lines"]
    assert isinstance(chart_lines, list)
    assert chart_lines[0] == "OMN-9359 Demo Cost Comparison"
    assert any("claude-opus-4-5" in line for line in chart_lines)
    assert chart_lines[-1] == "Cheapest: onex-deterministic"
