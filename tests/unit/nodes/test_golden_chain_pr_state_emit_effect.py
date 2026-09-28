# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observation -> real spool/enrichment -> bus adapter -> projection SQL (OMN-19999)."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_pr_state_emit import (
    HandlerPrStateEmit,
)
from omnimarket.nodes.node_pr_state_emit_effect.models.enum_pr_state import EnumPrState
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event
from tests.unit.nodes.node_projection_pr_state.test_pr_state_writer import (
    FakeDb,
    writer,
)

pytestmark = pytest.mark.unit


class RecordingAdapter:
    def __init__(self) -> None:
        self.received: list[tuple[str, dict[str, object], str | None]] = []

    def publish(
        self,
        topic: str,
        payload: object,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        assert isinstance(payload, dict)
        self.received.append((topic, payload, key))


def test_observation_to_bus_to_projection_sql(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    handler = HandlerPrStateEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    observations = [
        event(),
        event(
            state=EnumPrState.MERGED,
            observed_at="2026-09-28T11:00:00Z",
            merged_at="2026-09-28T11:00:00Z",
        ),
    ]
    for e in observations:
        assert handler.handle(e).published
    db = FakeDb()
    w = writer(db)
    for topic, payload, key in adapter.received:
        assert key == "OmniNode-ai/omnimarket#3050"
        assert topic == w.subscribe_topics[0]
        assert payload["correlation_id"] == payload["digest"]
        assert w.handle({**payload, "_topic": topic}) == {"rows_upserted": 1}
    assert len(db.statements) == 2
    assert db.statements[0][1][2] == "open"
    assert db.statements[1][1][2] == "merged"


def test_projection_terminal_contract() -> None:
    import yaml

    path = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_projection_pr_state/contract.yaml"
    )
    contract = yaml.safe_load(path.read_text())
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.projection-pr-state-applied.v1"
    )
