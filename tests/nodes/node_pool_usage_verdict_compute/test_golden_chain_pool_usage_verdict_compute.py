# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607: pool readings -> HandlerPoolUsageVerdict -> one verdict per label."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

import omnimarket.nodes.node_pool_usage_verdict_compute as node_package
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.handler_pool_usage_verdict import (
    HandlerPoolUsageVerdict,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolUsageRequest,
    ModelPoolUsageResult,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
TERMINAL_TOPIC = "onex.evt.omnimarket.pool-usage-verdict-evaluated.v1"

TOKEN_LIKE = "sk-ant-api03-tok-should-never-surface"

REQUEST_JSON = json.dumps(
    {
        "now": "2026-12-01T12:00:00Z",
        "correlation_id": "00000000-0000-4000-8000-000000000094",
        "policy": {
            "five_hour": {"drain_at": 0.9, "limited_at": 1.0},
            "weekly": {"drain_at": 0.85, "limited_at": 1.0},
            "max_reading_age_seconds": 900,
        },
        "readings": [
            {
                "label": "alpha-ok",
                "read_at": "2026-12-01T11:59:00Z",
                "five_hour_utilization": 0.4,
                "five_hour_resets_at": "2026-12-01T14:00:00Z",
                "seven_day_utilization": 0.3,
                "seven_day_resets_at": "2026-12-04T00:00:00Z",
            },
            {
                "label": "bravo-drain",
                "read_at": "2026-12-01T11:59:00Z",
                "five_hour_utilization": 0.93,
                "five_hour_resets_at": "2026-12-01T13:00:00Z",
                "seven_day_utilization": 0.5,
                "seven_day_resets_at": "2026-12-04T00:00:00Z",
            },
            {
                "label": "charlie-mark",
                "read_at": "2026-12-01T11:58:00Z",
                "five_hour_utilization": 0.2,
                "five_hour_resets_at": "2026-12-01T15:00:00Z",
                "seven_day_utilization": 0.1,
                "seven_day_resets_at": "2026-12-04T00:00:00Z",
                "limited_until": "2026-12-01T13:30:00Z",
            },
        ],
    }
)


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerPoolUsageVerdict
    assert issubclass(node_package.NodePoolUsageVerdictCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelPoolUsageRequest
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert (
        'node_pool_usage_verdict_compute = "omnimarket.nodes.'
        'node_pool_usage_verdict_compute"' in (ROOT / "pyproject.toml").read_text()
    )


def test_golden_chain_json_request_to_one_verdict_per_label() -> None:
    request = ModelPoolUsageRequest.model_validate_json(REQUEST_JSON)
    result = node_package.NodePoolUsageVerdictCompute().handle(request)

    verdicts = {u.label: u.verdict for u in result.labels}
    assert verdicts == {
        "alpha-ok": "ok",
        "bravo-drain": "drain",
        "charlie-mark": "limited",
    }
    assert result.drain_labels == ("bravo-drain",)
    assert (result.ok, result.drain, result.limited, result.unknown) == (1, 1, 1, 0)
    assert TOKEN_LIKE not in result.model_dump_json()

    wire = ModelPoolUsageResult.model_validate_json(result.model_dump_json())
    assert wire == result


def test_golden_chain_over_the_inmemory_bus_command_topic_to_terminal_event() -> None:
    """The contract's command topic carries the request; the terminal topic carries the verdicts."""
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    node = node_package.NodePoolUsageVerdictCompute()
    terminal: list[str] = []

    async def run() -> None:
        bus = EventBusInmemory()
        await bus.start()

        async def collect(message: object) -> None:
            terminal.append(bytes(message.value).decode())  # type: ignore[attr-defined]

        async def serve(message: object) -> None:
            request = ModelPoolUsageRequest.model_validate_json(
                bytes(message.value)  # type: ignore[attr-defined]
            )
            await bus.publish(
                terminal_topic, None, node.handle(request).model_dump_json().encode()
            )

        await bus.subscribe(terminal_topic, on_message=collect, group_id="golden")
        await bus.subscribe(command_topic, on_message=serve, group_id="node")
        await bus.publish(command_topic, None, REQUEST_JSON.encode())
        await bus.close()

    asyncio.run(run())

    assert len(terminal) == 1
    direct = node.handle(ModelPoolUsageRequest.model_validate_json(REQUEST_JSON))
    assert ModelPoolUsageResult.model_validate_json(terminal[0]) == direct
    assert direct.drain_labels == ("bravo-drain",)
