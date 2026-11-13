# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the friction step's paged read (OMN-23606): the request it sends, the answer it reads, the report it writes.

Each test fails when either side changes shape. The sender side is ``ModelPointQuery`` and the point-read adapter's request
builder (shared with the delegation scenario reader, the readiness verdict and the floor delegation reader); the receiver side
is the read node's own request and answer models, run in process on the in-memory reader, the handler and the answer bound the
deployed node runs. The reading on the real broker is the step run against the dev lane, recorded in the pull request.

Golden chain: a page the step asks for is a request the node's model accepts, the node's answer is a typed answer the adapter
reads, and following the cursor reads every matched row once. Error chain: a refusal or an error answer is an error the step
reports as unread, never rows; a request without the new fields is the request the other readers always sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerAnswerBound,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_friction_rollup_source import (
    _read_pages,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_friction_rollup_source import (
    ModelFrictionLedgerRead,
    ModelFrictionRollupStepSettings,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    _request,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
SINCE = datetime(2026, 11, 9, 3, 0, 0, tzinfo=UTC)
FRICTION = tuple(
    f"2026-11-09T10:{minute:02d}:00Z | FRICTION | lane=lane-{minute} | class=c{minute % 3} | cost=~5 minutes | row {minute}"
    for minute in range(23)
)
RESOLVED = "2026-11-09T11:00:00Z | STATUS | lane=alarm | resolved=c1 | healthy again"
NOISE = tuple(
    f"2026-11-09T12:{minute:02d}:00Z | STATUS | lane=lane-{minute} | a status line"
    for minute in range(10)
)


def _node(bound: int = 917516) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: NOW)
    for number, raw in enumerate((*FRICTION, RESOLVED, *NOISE)):
        assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})
    node = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)
    node._answer_bound = ModelWorkLedgerAnswerBound(
        broker_max_request_size=bound + 1000,
        envelope_headroom_bytes=0,
        max_answer_bytes=bound,
    )
    return node


class NodePort:
    """The point-read port over the node in process: the request the adapter would put on the wire, validated as the node's
    own request model, and the node's answer read back through the adapter's own answer reader."""

    def __init__(self, node: HandlerWorkLedgerQuery) -> None:
        self._node = node
        self.wire: list[dict[str, object]] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        correlation_id = str(uuid4())
        wire = _request(query, correlation_id)
        self.wire.append(wire)
        request = ModelWorkLedgerQueryRequest.model_validate(
            wire
        )  # the sender's shape is the node's
        answer = self._node.handle(request)
        return answer_from_json(json.loads(answer.model_dump_json()), correlation_id)


def _settings(**changes: object) -> ModelFrictionRollupStepSettings:
    base: dict[str, object] = {
        "ledger_read_command_env": "ONEX_LEDGER_BUS_READ_COMMAND",
        "ledger_page_timeout_seconds": 60,
        "ledger_page_size": 10,
        "ledger_max_pages": 12,
        "ledger_time_budget_seconds": 80,
        "ledger_reads": (
            ModelFrictionLedgerRead(kinds=("FRICTION",), why="class observations"),
            ModelFrictionLedgerRead(
                kinds=("STATUS",), term="resolved=", why="resolutions"
            ),
        ),
        "signals_unread_reason": "not read",
        "window_hours": 24,
        "bucket_hours": 2,
        "umbrella_tickets": (),
        "class_overlay_id": "x",
        "class_overlay_file_env": "Y",
    }
    return ModelFrictionRollupStepSettings.model_validate({**base, **changes})


def _read(
    port: NodePort, index: int, settings: ModelFrictionRollupStepSettings
) -> set[str]:
    return _read_pages(
        settings.ledger_reads[index],
        index + 1,
        settings,
        since=SINCE,
        until=NOW,
        argv=[],
        source=port,
        started=0.0,
        monotonic=lambda: 0.0,
    )


def test_golden_the_step_request_is_a_request_the_read_node_accepts_and_pages_to_every_matched_row() -> (
    None
):
    port = NodePort(_node())
    lines = _read(port, 0, _settings())
    assert lines == set(FRICTION)
    # 23 rows at 10 a page: three requests, the later two carrying the cursor the node gave.
    assert [("cursor" in wire) for wire in port.wire] == [False, True, True]
    assert all(wire["until"] == "2026-11-10T03:00:00Z" for wire in port.wire)


def test_golden_the_term_narrows_the_status_rows_to_the_resolved_cell_in_the_node() -> (
    None
):
    port = NodePort(_node())
    assert _read(port, 1, _settings()) == {RESOLVED}
    assert port.wire[0]["term"] == "resolved="


def test_golden_a_query_without_until_or_term_is_the_request_the_other_readers_sent() -> (
    None
):
    wire = _request(
        ModelPointQuery(
            query="rows",
            lanes=("a",),
            since=SINCE,
            kinds=("STATUS",),
            page_size=500,
            cursor="c",
        ),
        "id-1",
    )
    assert wire == {
        "correlation_id": "id-1",
        "query": "rows",
        "lanes": ["a"],
        "since": "2026-11-09T03:00:00Z",
        "kinds": ["STATUS"],
        "page_size": 500,
        "cursor": "c",
    }


def test_error_a_page_over_the_nodes_bound_is_an_unread_answer_not_rows() -> None:
    port = NodePort(_node(bound=1500))
    answer = port.read(
        ModelPointQuery(query="rows", since=SINCE, kinds=("FRICTION",), page_size=20)
    )
    assert answer.lines == ()
    assert answer.error is not None
    assert answer.error.startswith("node-error")


def test_error_a_cursor_the_node_did_not_give_is_an_error_the_step_reports_unread() -> (
    None
):
    from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
        PublishRefusedError,
    )

    class Bad:
        def read(self, query: ModelPointQuery) -> ModelPointAnswer:
            return ModelPointAnswer(error="node-error:cursor is not a page cursor")

    with pytest.raises(PublishRefusedError, match="is unread"):
        _read_pages(
            _settings().ledger_reads[0],
            1,
            _settings(),
            since=SINCE,
            until=NOW,
            argv=[],
            source=Bad(),
            started=0.0,
            monotonic=lambda: 0.0,
        )
