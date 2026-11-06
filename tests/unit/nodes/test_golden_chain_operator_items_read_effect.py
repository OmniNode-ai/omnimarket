# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live operator-item source reaches the shared renderer over discovered bus routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_ask_state import ModelAskStateRequest
from omnimarket.models.model_operator_item_render import (
    ModelOperatorItemRenderRequest,
    ModelOperatorItemsReadRequest,
)
from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute
from omnimarket.nodes.node_operator_items_read_effect import NodeOperatorItemsReadEffect
from tests.nodes.node_operator_item_render_compute.test_item_render import (
    exchange,
    render_request,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("unread", [False, True])
async def test_golden_chain_operator_items_read_effect_renders_live_or_unknown(
    tmp_path: Path, unread: bool
) -> None:
    raw = json.loads(
        (
            ROOT / "tests/unit/nodes/node_ask_state_compute/fixtures/tracked_items.json"
        ).read_text()
    )
    raw["config"] = yaml.safe_load(NodeAskStateCompute.contract_path.read_text())[
        "config"
    ]
    ledger = tmp_path / "ledger.md"
    if not unread:
        ledger.write_text("\n".join(raw["rows"]))
    source = ModelOperatorItemsReadRequest(
        ledger_path=ledger,
        fold=ModelAskStateRequest.model_validate(raw),
        config=render_request().config,
    )
    contract = yaml.safe_load(NodeOperatorItemsReadEffect.contract_path.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.operator-items-read-completed.v1"
    )
    bus = EventBusInmemory(environment="local", group="operator-items-read-chain")
    await bus.start()
    try:
        snapshot = await exchange(bus, source, "node_operator_items_read_effect")
        rendered = await exchange(
            bus, ModelOperatorItemRenderRequest.model_validate(snapshot)
        )
    finally:
        await bus.close()
    if unread:
        assert snapshot["report"] is None
        assert "FileNotFoundError" in snapshot["source_error"]
        assert rendered["known"] is False
        assert rendered["count"] is None
        assert rendered["lines"][0].startswith("UNKNOWN open-items")
    else:
        assert snapshot["source_error"] is None
        assert snapshot["report"]["asks"]
        assert rendered["known"] is True
        assert rendered["count"] > 0
        assert rendered["lines"][0].startswith("OPEN-ITEMS")
