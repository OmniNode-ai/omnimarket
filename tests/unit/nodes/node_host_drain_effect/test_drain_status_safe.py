# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The status line of the drain verb (OMN-24489): SAFE is a reading of the host itself.

SAFE to power down needs every host read (the host's lanes, units and model servers) and this machine's runner receipts
to read 0, twice, on a drain mark that outlives the process. A count above 0, a read that failed, a host with no host
read declared and a mark held in memory are each NOT SAFE. Each test below fails on a handler that counts only the
runner receipts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FixedHostRead,
    FixedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import (
    InMemoryDrainStore,
    JsonlDrainStore,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    EnumHostReadKind,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolHostRead,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
HOST = "example-host-b"


class _UnreadableLanes:
    """A lane source whose count cannot be read: an unknown count is never safe."""

    def running(self, host_name: str) -> int:
        raise OSError("the lane count command failed")


class _Appearing:
    """A host read that shows nothing on its first pass and one busy thing on the second: a lane that started between."""

    def __init__(self, kind: EnumHostReadKind) -> None:
        self.name = f"appearing-{kind.value}"
        self.kind = kind
        self.calls = 0

    def read(self, host_name: str) -> int:
        self.calls += 1
        return 0 if self.calls == 1 else 1


def _reads(
    *,
    lanes: int = 0,
    units: int = 0,
    model_servers: int = 0,
    unreadable: frozenset[str] = frozenset(),
) -> list[ProtocolHostRead]:
    counts = {
        EnumHostReadKind.LANES: lanes,
        EnumHostReadKind.UNITS: units,
        EnumHostReadKind.MODEL_SERVERS: model_servers,
    }
    return [
        FixedHostRead(
            kind.value,
            kind,
            {HOST: count, "example-host-a": count},
            unreadable=unreadable,
        )
        for kind, count in counts.items()
    ]


def _handler(
    overlay: ModelHostDrainOverlay,
    store: ProtocolDrainStore,
    lanes: Any,
    sink: InMemoryEmissionSink,
    host_reads: Sequence[ProtocolHostRead],
    clock: Callable[[], datetime] = lambda: NOW,
) -> HandlerHostDrain:
    return HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=clock,
        backend="in_memory",
        host_reads=host_reads,
    )


def _durable(tmp_path: Path) -> JsonlDrainStore:
    return JsonlDrainStore(tmp_path / "drain.jsonl", clock=lambda: NOW)


def _drain(handler: HandlerHostDrain, host: str = HOST) -> Any:
    return handler.handle(ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name=host))


def _until(overlay: ModelHostDrainOverlay) -> str:
    return (NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_every_read_zero_on_a_durable_mark_is_safe(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    result = _drain(_handler(overlay, _durable(tmp_path), lanes, sink, _reads()))
    assert result.outcome is EnumDrainOutcome.DRAINING
    assert result.safe is True
    assert result.drained is True
    assert result.status_line == (
        f"{HOST} draining until {_until(overlay)}: 0 lane(s) running "
        f"({overlay.drain.lane_count_scope}); host read lanes=0 units=0 model_servers=0: SAFE to power down"
    )


@pytest.mark.parametrize(
    "busy", ["lanes", "units", "model_servers"], ids=lambda kind: f"host-{kind}"
)
def test_a_busy_host_is_not_safe_whatever_the_receipts_say(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
    busy: str,
) -> None:
    """The runner receipts read 0 (this machine launched nothing there) while the host itself shows work."""
    result = _drain(
        _handler(overlay, _durable(tmp_path), lanes, sink, _reads(**{busy: 3}))
    )
    assert result.lanes_running == 0
    assert result.safe is False
    assert result.drained is False
    assert f"{busy}=3" in result.status_line
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_an_unreadable_host_read_is_never_safe(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    result = _drain(
        _handler(
            overlay,
            _durable(tmp_path),
            lanes,
            sink,
            _reads(unreadable=frozenset({HOST})),
        )
    )
    assert result.safe is False
    assert all(read.count is None for read in result.host_reads)
    assert "lanes=unreadable" in result.status_line
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_a_host_with_no_host_read_declared_is_not_safe(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    result = _drain(_handler(overlay, _durable(tmp_path), lanes, sink, ()))
    assert result.safe is False
    assert "host read none declared" in result.status_line
    assert "the host itself was not read" in result.detail


def test_a_mark_held_in_memory_is_not_safe(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    """The in_memory profile's mark dies with the process: a SAFE line on it would outlive its own evidence."""
    result = _drain(
        _handler(overlay, InMemoryDrainStore(clock=lambda: NOW), lanes, sink, _reads())
    )
    assert result.draining is True
    assert result.safe is False
    assert result.status_line.endswith(": NOT SAFE to power down")
    assert "held in memory only" in result.detail


def test_an_unreadable_receipt_count_is_never_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink, tmp_path: Path
) -> None:
    result = _drain(
        _handler(overlay, _durable(tmp_path), _UnreadableLanes(), sink, _reads())
    )
    assert result.lanes_running is None
    assert result.safe is False
    assert result.status_line == (
        f"{HOST} draining until {_until(overlay)}: lane count unreadable; "
        "host read lanes=0 units=0 model_servers=0: NOT SAFE to power down"
    )


def test_a_lane_that_starts_between_the_two_passes_is_not_safe(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    """One zero is a moment; SAFE needs the host to read 0 on two passes around the mark read."""
    result = _drain(
        _handler(
            overlay,
            _durable(tmp_path),
            lanes,
            sink,
            [_Appearing(EnumHostReadKind.UNITS)],
        )
    )
    assert result.safe is False
    assert result.host_reads[0].count == 1


def test_a_mark_that_expires_during_the_status_prints_one_consistent_line(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    """The store's own clock steps past the mark's end between two reads; the line must not say 'until unknown'."""
    steps = {"n": 0}

    def stepping() -> datetime:
        steps["n"] += 1
        return NOW if steps["n"] <= 2 else NOW + timedelta(days=30)

    store = JsonlDrainStore(tmp_path / "drain.jsonl", clock=stepping)
    handler = _handler(overlay, store, lanes, sink, _reads())
    _drain(handler)
    result = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name=HOST)
    )
    assert result.draining is True
    assert "until unknown" not in result.status_line
    assert f"until {_until(overlay)}" in result.status_line


def test_the_mark_ends_when_the_clock_passes_it(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    now = {"t": NOW}
    handler = _handler(
        overlay,
        _durable(tmp_path),
        lanes,
        sink,
        _reads(),
        clock=lambda: now["t"],
    )
    _drain(handler)
    now["t"] = NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)
    result = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name=HOST)
    )
    assert result.draining is False
    assert result.safe is False
    assert result.until is None
    assert result.status_line.startswith(f"{HOST} not draining:")


def test_a_not_draining_host_says_so_with_its_counts(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
    tmp_path: Path,
) -> None:
    handler = _handler(overlay, _durable(tmp_path), lanes, sink, _reads())
    status = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name="example-host-a")
    )
    assert status.draining is False
    assert status.until is None
    assert status.safe is False
    assert status.status_line == (
        f"example-host-a not draining: 2 lane(s) running ({overlay.drain.lane_count_scope}); "
        "host read lanes=0 units=0 model_servers=0"
    )
    cleared = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.UNDRAIN, host_name="example-host-a")
    )
    assert cleared.status_line == status.status_line


def test_a_not_draining_host_with_an_unreadable_count_says_so(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink, tmp_path: Path
) -> None:
    result = _handler(
        overlay, _durable(tmp_path), _UnreadableLanes(), sink, _reads()
    ).handle(ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name="example-host-a"))
    assert result.draining is False
    assert result.lanes_running is None
    assert result.status_line == (
        "example-host-a not draining: lane count unreadable; "
        "host read lanes=0 units=0 model_servers=0"
    )
