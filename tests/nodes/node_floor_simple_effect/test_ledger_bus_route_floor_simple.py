# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor reads its ledger facts from the bus-fed projection, never from the ledger file (OMN-19609).

The contract's ledger route is the read command the environment names (node_work_ledger_query_effect over the
bus), not the ledger file. Each read over the bus is one round trip to the query node (18 s to 32 s measured on
h053 on 2026-11-07), and the floor makes one read for the go, one per (row type, lane) of the piece event rules
and one for the readings, so the reads run at once and the answer is the same as when they run one by one.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.floor_17a import ModelFloorSimpleOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    ModelFloorSimpleRuntime,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    read_ledger_facts,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
    InMemoryLedger,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_simple_effect/contract.yaml"
)
FIXTURE = Path(__file__).parent / "fixtures" / "ledger_rows_go_and_code_graph.txt"
AT = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)


def runtime() -> ModelFloorSimpleRuntime:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelFloorSimpleRuntime.model_validate(contract["runtime"])


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def lines() -> list[str]:
    return [x for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]


class _SlowLedger:
    """The in-memory ledger behind a delay, counting how many reads are in flight at once."""

    def __init__(self, delay: float) -> None:
        self._inner = InMemoryLedger(lines())
        self._delay = delay
        self._lock = threading.Lock()
        self._in_flight = 0
        self.most_in_flight = 0
        self.reads = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        with self._lock:
            self._in_flight += 1
            self.reads += 1
            self.most_in_flight = max(self.most_in_flight, self._in_flight)
        try:
            time.sleep(self._delay)
            return self._inner.read(query)
        finally:
            with self._lock:
                self._in_flight -= 1


def test_bus_route_the_contract_reads_the_projection_through_the_bus_read_command() -> (
    None
):
    rt = runtime()
    assert isinstance(rt.ledger, ModelCommandLedgerReadRoute)
    assert rt.ledger.command_env == "ONEX_LEDGER_BUS_READ_COMMAND"
    assert rt.ledger_parallel_reads > 1
    source = build_row_source(
        rt.ledger, {"ONEX_LEDGER_BUS_READ_COMMAND": "onex ledger query --stdin"}
    )
    assert isinstance(source, CommandLedgerRowSource)


def test_bus_route_the_reads_run_at_once_and_answer_as_one_by_one() -> None:
    ov = overlay()
    one_by_one = read_ledger_facts(InMemoryLedger(lines()), ov, AT)
    slow = _SlowLedger(delay=0.2)
    at_once = read_ledger_facts(slow, ov, AT, parallel_reads=8)
    assert slow.reads > 2
    assert slow.most_in_flight > 1
    assert at_once == one_by_one
    assert at_once.facts is not None
    assert at_once.facts.go_given_through == 6


def test_bus_route_a_failed_read_among_parallel_reads_is_unread_never_empty() -> None:
    broken = InMemoryLedger(lines())
    broken.read_error = "read-command-exit-1:TimeoutError"
    result = read_ledger_facts(broken, overlay(), AT, parallel_reads=8)
    assert result.facts is None
    assert result.error == "go rulings unread: read-command-exit-1:TimeoutError"
