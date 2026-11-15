# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the work-state panel reducer, with no event published (OMN-19998)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.work_state_panel import EnumPanelRefusal
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    handler_work_state_panel_reducer as handler_module,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.service_work_state_panel_reducer import (
    body_from_event,
    publication_id,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import decode
from tests.nodes.node_work_state_panel_reducer.harness import StateIoRowStore, run_event
from tests.nodes.node_work_state_panel_reducer.support import NOW, T0, event, overlay

pytestmark = pytest.mark.unit

CONTRACT = Path(handler_module.__file__).resolve().parent.parent / "contract.yaml"


def handler() -> HandlerWorkStatePanelReducer:
    return HandlerWorkStatePanelReducer(clock=lambda: NOW, overlay=overlay())


def test_golden_chain_an_accepted_event_ends_in_a_saved_row() -> None:
    store = StateIoRowStore()
    produced = event(100)
    run_event(store, handler(), produced)
    row = decode(store.rows["file_sources"][0])
    assert row.epoch == 1
    assert row.publication_id == publication_id(body_from_event(produced))
    assert row.body is not None
    assert row.body.as_of == T0 + timedelta(seconds=100)
    assert row.last_refusal_code is None
    assert store.saves == [0]


def test_error_chain_each_refusal_ends_in_its_named_outcome() -> None:
    store = StateIoRowStore()
    run_event(store, handler(), event(100, counts={"bogus": 1}))
    assert (
        decode(store.rows["file_sources"][0]).last_refusal_code
        is EnumPanelRefusal.UNDECODABLE
    )
    run_event(store, handler(), event(100))
    run_event(store, handler(), event(100000))
    row = decode(store.rows["file_sources"][0])
    assert row.last_refusal_code is EnumPanelRefusal.FUTURE_DATED
    assert row.epoch == 1
    assert row.body is not None


def test_error_chain_a_key_mismatch_saves_nothing() -> None:
    store = StateIoRowStore()
    run_event(
        store, handler(), event(100, projector=EnumWorkStateProjectorProfile.UNREAD)
    )
    assert store.rows == {}
    assert store.saves == []


def test_contract_shape() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["state_io"]["key"] == "projector"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.work-state-projected.v1"
    ]
    assert "published_events" not in contract
    assert not contract["event_bus"].get("publish_topics")
