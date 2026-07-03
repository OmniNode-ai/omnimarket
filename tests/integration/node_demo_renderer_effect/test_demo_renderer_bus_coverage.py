# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full I/O-boundary EFFECT coverage for node_demo_renderer_effect, driven over
the canonical in-memory bus.

OMN-13674 (cluster side-effect-alert-render-discovery, archetype effect). A
``ModelDemoRenderRequest`` lands on the declared subscribe topic
``onex.evt.omnimarket.demo-cost-computed.v1`` and the terminal
``ModelDemoRenderResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.demo-chart-rendered.v1`` by ``LocalRuntimeBusAdapter``. No
live Kafka / ``.201``.

This effect's only side-effect boundary is ``stdout`` (``sys.stdout.write``);
it performs no network, subprocess, or database I/O, so there is nothing to
constructor-inject and nothing to monkeypatch — the boundary is observed
directly through pytest ``capsys``. Because the render is a pure transform of
cost data with no external system, it has no failure / retry / gate-blocked
mode; that is recorded honestly here rather than faked.

Declared-state coverage (contract ``outputs`` + ``event_bus.publish_topics``):
  * ``chart_lines`` — asserted off every terminal event;
  * ``onex.evt.omnimarket.demo-chart-rendered.v1`` — the terminal topic the
    result is published onto over the bus.

Branch coverage at the render boundary:
  * multi-model scaling (``max_cost > 0``, ``total_cost_usd > 0`` -> filled bar,
    min-one-hash rule);
  * zero-cost entry (``total_cost_usd == 0`` -> empty bar) alongside priced ones;
  * empty cost list (``max_cost == 0`` -> no bars, ``cheapest`` renders ``n/a``);
  * custom ``bar_width`` and ``title``;
  * idempotency: identical input yields identical ``chart_lines`` and stdout.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.events.demo import ModelDemoCostEntry, ModelDemoCostResult
from omnimarket.nodes.node_demo_renderer_effect.handlers.handler_renderer import (
    NodeDemoRendererEffect,
)
from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
    ModelDemoRenderRequest,
    ModelDemoRenderResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.evt.omnimarket.demo-cost-computed.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.demo-chart-rendered.v1"


def _entry(
    model_id: str, total: float, prompt: int = 100, completion: int = 50
) -> ModelDemoCostEntry:
    return ModelDemoCostEntry(
        model_id=model_id,
        prompt_cost_usd=round(total * 0.6, 6),
        completion_cost_usd=round(total * 0.4, 6),
        total_cost_usd=total,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )


async def _drive(bus: Any, request: ModelDemoRenderRequest) -> ModelDemoRenderResult:
    adapter = LocalRuntimeBusAdapter(
        handler=NodeDemoRendererEffect(),
        handler_name="demo-renderer",
        input_model_cls=ModelDemoRenderRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-demo-renderer-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=request.model_dump_json().encode("utf-8")
    )
    history = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(history) == 1, f"expected exactly one terminal event, got {history}"
    assert history[-1].topic == TOPIC_COMPLETED
    return ModelDemoRenderResult.model_validate(json.loads(history[-1].value))


# ---------------------------------------------------------------------------
# multi-model scaling: highest cost fills the full bar, min-one-hash rule.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_multi_model_scaling_over_bus(
    integration_event_bus: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        cost = ModelDemoCostResult(
            costs=[
                _entry("expensive-model", 0.100000),
                _entry("cheap-model", 0.000100),
            ],
            cheapest_model_id="cheap-model",
        )
        result = await _drive(
            bus, ModelDemoRenderRequest(cost_result=cost, bar_width=40)
        )
        chart_lines = result.chart_lines
        assert chart_lines[0] == "Model Cost Comparison"
        # The most expensive model gets a full-width bar.
        expensive_line = next(line for line in chart_lines if "expensive-model" in line)
        assert expensive_line.count("#") == 40
        # The cheap model still gets at least one hash (min-one rule for cost>0).
        cheap_line = next(line for line in chart_lines if "cheap-model" in line)
        assert 1 <= cheap_line.count("#") < 40
        assert chart_lines[-1] == "Cheapest: cheap-model"
        # stdout side-effect observed at the boundary.
        assert "expensive-model" in capsys.readouterr().out
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# zero-cost entry: total_cost_usd == 0 -> empty bar alongside priced entries.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_zero_cost_entry_empty_bar_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        cost = ModelDemoCostResult(
            costs=[
                _entry("priced-model", 0.05),
                _entry("free-model", 0.0),
            ],
            cheapest_model_id="free-model",
        )
        result = await _drive(bus, ModelDemoRenderRequest(cost_result=cost))
        free_line = next(line for line in result.chart_lines if "free-model" in line)
        # Zero cost -> filled == 0 -> no hash marks in the bar.
        assert free_line.count("#") == 0
        # Tokens are still summed and shown (100 prompt + 50 completion).
        assert "150 tokens" in free_line
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# empty cost list: max_cost == 0, cheapest None -> no bars, "n/a" footer.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_empty_costs_renders_na_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        cost = ModelDemoCostResult(costs=[], cheapest_model_id=None)
        result = await _drive(bus, ModelDemoRenderRequest(cost_result=cost))
        # Only the title, underline, and the cheapest footer are rendered.
        assert result.chart_lines[0] == "Model Cost Comparison"
        assert result.chart_lines[-1] == "Cheapest: n/a"
        assert len(result.chart_lines) == 3
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# custom bar_width + title threaded through to the render.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_custom_bar_width_and_title_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        cost = ModelDemoCostResult(
            costs=[_entry("only-model", 0.02)],
            cheapest_model_id="only-model",
        )
        result = await _drive(
            bus,
            ModelDemoRenderRequest(cost_result=cost, bar_width=10, title="Cost Report"),
        )
        assert result.chart_lines[0] == "Cost Report"
        assert result.chart_lines[1] == "-" * len("Cost Report")
        only_line = next(line for line in result.chart_lines if "only-model" in line)
        # Single priced model scales to the full custom width.
        assert only_line.count("#") == 10
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# idempotency: identical input yields identical chart_lines and stdout.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_identical_input_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    cost = ModelDemoCostResult(
        costs=[_entry("a", 0.03), _entry("b", 0.01)],
        cheapest_model_id="b",
    )
    request = ModelDemoRenderRequest(cost_result=cost)
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(bus, request)
            payloads.append(json.dumps(result.chart_lines))
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
