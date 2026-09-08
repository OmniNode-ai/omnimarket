# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The one state ``node_projection_session_replay`` declares as an output.

``onex.evt.omnimarket.projection-session-replay-applied.v1`` is the node's
entire declared output surface: its single ``event_bus.publish_topics`` entry
and, since OMN-18013, its single ``externally_consumed_topics`` sink. Nothing in
this repo's test corpus asserted it, so the contract-state-coverage gate carried
the node as a baselined gap.

OMN-17888 rewrote ``HandlerProjectionSessionReplay.project`` (the whole session
was being read back on every event), which makes the gap this lane's to close:
the baseline is a grandfather clause for untouched legacy debt, not an
exemption for a node somebody is actively changing.

What is worth asserting here is the COUPLING, not the string. The runtime does
not emit that terminal event because the handler returned without raising -- it
emits it only when ``rows_upserted >= 1``
(``handler_wiring._make_projection_dispatch_callback``, OMN-13360), and logs an
ERROR at zero. So the state is covered by proving that the handler's own
``rows_upserted`` tracks whether a row was actually durably written, on both the
first delivery and a redelivery, and that the contract routes it to exactly the
declared topic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TABLE,
    TOPIC_SESSION_STARTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelSessionReplayEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

PROJECTION_SESSION_REPLAY_APPLIED = (
    "onex.evt.omnimarket.projection-session-replay-applied.v1"
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_session_replay"
    / "contract.yaml"
)


def _contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_the_node_declares_exactly_one_output_state() -> None:
    """One subscribe fan-in, one terminal event out.

    Asserted as an equality rather than a membership check: a second publish
    topic added without a test would silently re-open the coverage gap this
    file exists to close.
    """
    contract = _contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["publish_topics"] == [PROJECTION_SESSION_REPLAY_APPLIED]


def test_the_terminal_state_is_declared_as_a_graph_sink() -> None:
    """No node contract subscribes to it, so it must say so explicitly.

    OMN-18013 closed the contract topic graph: a publish topic with no
    in-graph subscriber is either a wiring bug or a deliberate exit, and the
    contract has to state which.
    """
    contract = _contract()
    assert contract["externally_consumed_topics"] == [PROJECTION_SESSION_REPLAY_APPLIED]


def test_the_terminal_state_is_gated_on_a_row_actually_landing() -> None:
    """``rows_upserted`` is the gate, and it reports the write, not the call.

    The runtime emits ``onex.evt.omnimarket.projection-session-replay-applied.v1``
    only when the handler reports ``rows_upserted >= 1`` (OMN-13360), precisely
    so a handler that returns normally having written nothing cannot produce a
    false ``projected: true``. A store that refuses the write must therefore
    drive the count to zero.
    """
    handler = HandlerProjectionSessionReplay()
    event = ModelSessionReplayEvent(
        session_id="9787a4a3-ec49-4819-8bdc-5044efb94550",
        emitted_at="2026-09-07T15:53:00Z",
    )

    written = InmemoryDatabaseAdapter()
    assert handler.project(event, written, TOPIC_SESSION_STARTED).rows_upserted == 1, (
        "a durable write must report one row so the terminal event is emitted"
    )
    assert len(written.query(TABLE)) == 1

    class _RefusingAdapter(InmemoryDatabaseAdapter):
        def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
            return False

    refused = _RefusingAdapter()
    assert handler.project(event, refused, TOPIC_SESSION_STARTED).rows_upserted == 0, (
        "a refused write must report zero rows; reporting one would emit the "
        "terminal event for a row that does not exist"
    )
    assert refused.query(TABLE) == []


def test_a_redelivery_still_reports_a_row_so_the_terminal_event_repeats() -> None:
    """A repeated write is repeated deliberately, not skipped.

    Skipping the UPSERT on a redelivery would return zero rows, which the
    runtime reads as "wrote nothing", logs as an ERROR and refuses to emit the
    terminal event for -- so an idempotent redelivery would look like a failure.
    """
    handler = HandlerProjectionSessionReplay()
    adapter = InmemoryDatabaseAdapter()
    event = ModelSessionReplayEvent(
        session_id="9787a4a3-ec49-4819-8bdc-5044efb94550",
        emitted_at="2026-09-07T15:53:00Z",
    )

    first = handler.project(event, adapter, TOPIC_SESSION_STARTED)
    second = handler.project(event, adapter, TOPIC_SESSION_STARTED)

    assert first.rows_upserted == 1
    assert second.rows_upserted == 1
    assert len(adapter.query(TABLE)) == 1, "the redelivery appended a second row"
