# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the evaluate node's published batch reaches the board and alert consumers.

node_lab_sli_compute publishes ModelLabSliEvaluationBatch on
onex.evt.omnimarket.lab-sli-evaluated.v1; the board and alert nodes subscribe
that topic. Until this fix both consumers declared only their local request
models (ModelLabSliBoardRequest / ModelLabSliAlertRequest, both extra=forbid)
and no event_model on the route, so the runtime validated the topic payload
into a request model that has no `evaluation` key and every delivery raised a
ValidationError. This test builds the producer's batch, dumps it the way the
wire does, resolves each consumer's event model FROM ITS CONTRACT and drives
the real handlers, asserting state is carried between intervals.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.lab_sli import (
    ModelLabSliAlertRequest,
    ModelLabSliBoardRequest,
    ModelLabSliEvaluationBatch,
)
from omnimarket.nodes.node_lab_sli_alert_effect.handlers.handler_lab_sli_alert import (
    HandlerLabSliAlert,
)
from omnimarket.nodes.node_lab_sli_board_compute.handlers.handler_lab_sli_board import (
    HandlerLabSliBoard,
)
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
    load_sli_table,
)
from tests.nodes.node_lab_sli_compute.test_golden_chain_lab_sli import _night

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)


def _event_model(contract_path: Path) -> type:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    entry = contract["handler_routing"]["handlers"][0]
    dotted = entry.get("event_model") or contract["input_model"]
    module_name, class_name = dotted.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)


_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _board_contract() -> Path:
    return _NODES / "node_lab_sli_board_compute" / "contract.yaml"


def _alert_contract() -> Path:
    return _NODES / "node_lab_sli_alert_effect" / "contract.yaml"


def _producer_batch(start: datetime = T0) -> ModelLabSliEvaluationBatch:
    return HandlerLabSliEvaluate().handle(_night(start))


def _dumped(batch: ModelLabSliEvaluationBatch) -> dict:
    return json.loads(batch.model_dump_json())


def test_contract_event_models_read_the_published_batch() -> None:
    batch = _producer_batch()
    payload = _dumped(batch)
    board_model = _event_model(_board_contract())
    alert_model = _event_model(_alert_contract())
    assert board_model.model_validate(payload) is not None
    assert alert_model.model_validate(payload) is not None


def test_board_renders_every_indicator_from_the_batch() -> None:
    batch = _producer_batch()
    board = HandlerLabSliBoard().handle(batch)
    for definition in load_sli_table():
        assert definition.sli_id in board.html
        assert definition.sli_id in board.markdown


def test_alert_returns_a_result_from_the_batch() -> None:
    batch = _producer_batch()
    result = HandlerLabSliAlert().handle(batch)
    assert result.actions


def test_board_carries_state_across_intervals() -> None:
    handler = HandlerLabSliBoard()
    handler.handle(_producer_batch(T0))
    second = handler.handle(_producer_batch(T0 + timedelta(minutes=15)))
    intervals = {
        p.interval_start
        for p in second.projection.points
        if p.sli_id == "lane_placement_no_host_rate"
    }
    assert T0 in intervals
    assert T0 + timedelta(minutes=15) in intervals
    assert len(intervals) == 2


def test_alert_suppresses_what_the_first_fired() -> None:
    handler = HandlerLabSliAlert()
    batch = _producer_batch()
    first = handler.handle(batch)
    assert first.actions, "positive control: the first interval fires"
    second = handler.handle(batch)
    assert second.actions == ()
    assert second.suppressed


def test_old_request_models_cannot_read_the_topic() -> None:
    payload = _dumped(_producer_batch())
    with pytest.raises(ValidationError):
        ModelLabSliBoardRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        ModelLabSliAlertRequest.model_validate(payload)
