# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot counts running lanes from the ledger node's open-claims answer, not from the raw rows of the lookback (OMN-24417).

Measured on h053 on 2026-11-14: the lane rows read (TERMINAL, RELEASE and CLAIM rows of a six hour window, about two thousand rows) was refused by the
ledger node ("the rows answer would be 1095380 bytes against the bound of 917516 bytes") or ran past the 30 s read limit (TimeoutExpired), and the
pass ended failed with lanes_unread and hosts 0, as the Mac unit did. One open-claims query with claims_since answered 53 open claims, each with its host.

Golden: the answer's OPEN-CLAIM lines count per host (a lane once, a claim with no host skipped, both pages of a cursor read), the shipped redpanda
profile wires the source through the real bus read command route, and the real handler records the counts. Error: an answer error, a cursor that does
not advance, a refused read command and an unset command variable end in the typed lanes_unread failure or a refused wiring. Failing controls: the rows
read of the same stub is refused where the open-claims read answers; an overlay naming both reads, or neither, is refused.
"""

from __future__ import annotations

import json
import shlex
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.models.model_work_ledger_reader import ModelPointAnswer
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_sources import (
    LedgerOpenClaimsLanesSource,
    LedgerRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_wiring import (
    build_pool_snapshot_ports,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    EnumPoolSnapshotFailureReason,
    EnumPoolSnapshotStatus,
    ModelPoolSnapshotFailedEvent,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    ModelPoolSnapshotOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    accounts,
    advertisement,
    handler,
    host_row,
    overlay,
    pool_request,
)

pytestmark = pytest.mark.unit

PROFILE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_host_pool_snapshot_effect/overlay.redpanda.example.yaml"
)


def _claim(lane: str, host: str) -> str:
    return (
        f"OPEN-CLAIM lane={lane} claim=2026-11-14T08:00:00Z db:1 ticket=OMN-1 newest-row=2026-11-14T08:30:00Z "
        f"kind=STATUS age=30m rows=3 host={host} run=none leases=none liveness=unknown"
    )


_HEADER = "open-claims since=2026-11-14T03:00:00Z claims=5 open=5 ledger-newest=2026-11-14T08:50:00Z now=2026-11-14T09:00:00Z"


class ScriptedPointRead:
    """A point read answering one scripted answer per call, and logging each query."""

    def __init__(self, *answers: ModelPointAnswer) -> None:
        self._answers = list(answers)
        self.queries: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.queries.append(query)
        return self._answers.pop(0)


def test_golden_open_claims_count_per_host_over_two_pages() -> None:
    read = ScriptedPointRead(
        ModelPointAnswer(
            lines=(
                _HEADER,
                _claim("lane-a", "h1"),
                _claim("lane-b", "h1"),
                _claim("lane-n", "none"),
            ),
            next_cursor="c2",
        ),
        ModelPointAnswer(lines=(_claim("lane-a", "h2"), _claim("lane-c", "h2"))),
    )
    counts = LedgerOpenClaimsLanesSource(read, 6).running_lanes(T0)
    # lane-a counts once (its first claim line wins), lane-n names no host, the header line is no claim
    assert counts == {"h1": 2, "h2": 1}
    assert [q.query for q in read.queries] == ["open-claims", "open-claims"]
    assert [q.cursor for q in read.queries] == [None, "c2"]
    assert all(q.claims_since == T0 - timedelta(hours=6) for q in read.queries)


def test_error_an_answer_error_is_a_source_error_naming_the_cause() -> None:
    read = ScriptedPointRead(
        ModelPointAnswer(error="read-command-failed:TimeoutExpired")
    )
    with pytest.raises(
        PoolSnapshotSourceError,
        match="ledger open claims unread: read-command-failed:TimeoutExpired",
    ):
        LedgerOpenClaimsLanesSource(read, 6).running_lanes(T0)


def test_error_a_cursor_that_does_not_advance_is_refused_not_followed_forever() -> None:
    read = ScriptedPointRead(
        ModelPointAnswer(lines=(_claim("lane-a", "h1"),), next_cursor="c2"),
        ModelPointAnswer(lines=(_claim("lane-b", "h1"),), next_cursor="c2"),
    )
    with pytest.raises(PoolSnapshotSourceError, match="did not advance"):
        LedgerOpenClaimsLanesSource(read, 6).running_lanes(T0)
    assert len(read.queries) == 2


_STUB = """
import json, sys
request = json.loads(sys.stdin.read())
with open({log!r}, "a") as handle:
    handle.write(json.dumps(request) + "\\n")
if request["query"] != "open-claims":
    sys.stderr.write("Error: Work-ledger read refused (WorkLedgerQueryError): the rows answer would be over the bound\\n")
    sys.exit(1)
lines = {lines!r}
print(json.dumps({{"correlation_id": request["correlation_id"], "query": "open-claims", "status": "ok", "matched": len(lines), "lines": lines, "next_cursor": None}}))
"""


def _command(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "requests.jsonl"
    lines = [
        _HEADER,
        _claim("lane-a", "h1"),
        _claim("lane-b", "h1"),
        _claim("lane-c", "h2"),
    ]
    script = _STUB.format(log=str(log), lines=lines)
    return shlex.join([sys.executable, "-c", script]), log


def _open_claims_profile() -> ModelPoolSnapshotOverlay:
    return ModelPoolSnapshotOverlay.model_validate(
        yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    )


def test_golden_the_shipped_profile_wires_the_open_claims_read_through_the_bus_read_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    command, log = _command(tmp_path)
    ports = build_pool_snapshot_ports(
        _open_claims_profile(),
        environ={
            "EXAMPLE_LEDGER_READ_COMMAND": command,
            "ONEX_REGISTRY_ROOT": "/declared/registry",
        },
        publisher=InMemoryPoolSnapshotPublisher(),
    )
    assert isinstance(ports.lanes, LedgerOpenClaimsLanesSource)
    assert ports.lanes.running_lanes(T0) == {"h1": 2, "h2": 1}
    request = json.loads(log.read_text().splitlines()[0])
    assert request["query"] == "open-claims"
    assert request["claims_since"] == (T0 - timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert request["correlation_id"]


def test_error_wiring_refuses_an_unset_read_command_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    with pytest.raises(ValueError, match="open_claims_read route refused"):
        build_pool_snapshot_ports(
            _open_claims_profile(),
            environ={"ONEX_REGISTRY_ROOT": "/declared/registry"},
            publisher=InMemoryPoolSnapshotPublisher(),
        )


def _chain(
    lanes_source: object,
    store: InMemoryPoolSnapshotStore,
    publisher: InMemoryPoolSnapshotPublisher,
):
    h = handler(
        ads=(advertisement("h1"), advertisement("h2")),
        rows=(host_row("h1"), host_row("h2")),
        accs=(accounts("h1"), accounts("h2")),
        store=store,
        publisher=publisher,
        at=T0,
    )
    h._lanes_source = lanes_source
    return h


def test_golden_chain_open_claims_become_used_slots_on_the_recorded_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    command, _ = _command(tmp_path)
    ports = build_pool_snapshot_ports(
        _open_claims_profile(),
        environ={
            "EXAMPLE_LEDGER_READ_COMMAND": command,
            "ONEX_REGISTRY_ROOT": "/declared/registry",
        },
        publisher=InMemoryPoolSnapshotPublisher(),
    )
    store, publisher = InMemoryPoolSnapshotStore(), InMemoryPoolSnapshotPublisher()
    result = _chain(ports.lanes, store, publisher).handle(pool_request())
    assert result.status is EnumPoolSnapshotStatus.WRITTEN
    assert result.record is not None
    by_host = {row.host: row for row in result.record.hosts}
    assert by_host["h1"].running_lanes == 2
    assert by_host["h2"].running_lanes == 1
    assert [topic for topic, _ in publisher.published] == [overlay().snapshot_topic]


def test_error_chain_an_unreadable_open_claims_answer_ends_failed_with_lanes_unread() -> (
    None
):
    read = ScriptedPointRead(ModelPointAnswer(error="read-command-exit-1"))
    store, publisher = InMemoryPoolSnapshotStore(), InMemoryPoolSnapshotPublisher()
    result = _chain(LedgerOpenClaimsLanesSource(read, 6), store, publisher).handle(
        pool_request()
    )
    assert result.status is EnumPoolSnapshotStatus.FAILED
    assert result.failure_reason is EnumPoolSnapshotFailureReason.LANES_UNREAD
    assert store.snapshot is None
    assert store.record is None
    assert len(publisher.published) == 1
    topic, event = publisher.published[0]
    assert topic == overlay().failed_topic
    assert isinstance(event, ModelPoolSnapshotFailedEvent)
    assert "ledger open claims unread: read-command-exit-1" in event.detail


def test_failing_control_the_rows_read_of_the_same_command_is_refused_where_open_claims_answer(
    tmp_path: Path,
) -> None:
    command, _ = _command(tmp_path)
    rows = LedgerRunningLanesSource(
        CommandLedgerRowSource(shlex.split(command), 30, 20000), 6
    )
    with pytest.raises(
        PoolSnapshotSourceError, match="read-command-exit-1:WorkLedgerQueryError"
    ):
        rows.running_lanes(T0)


def test_failing_control_an_overlay_naming_both_reads_or_neither_is_refused() -> None:
    document = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    neither = {k: v for k, v in document.items() if k != "open_claims_read"}
    with pytest.raises(
        ValueError, match="names one of ledger_read or open_claims_read"
    ):
        ModelPoolSnapshotOverlay.model_validate(neither)
    both = {
        **document,
        "ledger_read": {
            "route": "command",
            "command_env": "X",
            "timeout_seconds": 30,
            "row_limit": 20000,
        },
    }
    with pytest.raises(
        ValueError, match="names one of ledger_read or open_claims_read"
    ):
        ModelPoolSnapshotOverlay.model_validate(both)
    only_rows = {k: v for k, v in both.items() if k != "open_claims_read"}
    assert ModelPoolSnapshotOverlay.model_validate(only_rows).open_claims_read is None
