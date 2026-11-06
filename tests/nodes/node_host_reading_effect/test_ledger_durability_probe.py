# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-durability probe on both backends, each verdict shown by a named break (OMN-22662).

The in-memory ledger is both the append route and the durable store. The command backend runs the
real adapters (the bus append command and the read command) against two small scripts: an append
command that keeps rows in a file and prints the bus receipt, and a read command that answers a
typed query from that file with ``projected_at``, the shape the lab read node answers.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostReadingCommand,
    ModelProbeReading,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_ledger_durability import (
    read_durability,
)
from omnimarket.nodes.node_host_reading_effect.overlay import (
    ModelDurabilityProbeSettings,
    ModelHostReadingOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
    CommandLedgerRowSource,
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelAppendOutcome,
    ModelLedgerQuery,
    ModelLedgerRead,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)
from tests.nodes.node_host_reading_effect.host_reading_support import (
    EXAMPLE,
    LEDGER_HOST,
    RUN_AT,
    World,
)

pytestmark = pytest.mark.unit

ACTOR = "node:node_host_reading_effect"


def durability_overlay(**changes: object) -> ModelHostReadingOverlay:
    """The example overlay with the ledger host also declaring the durability probe."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["durability"].update(changes)
    data["hosts"][0]["probes"].append(EnumHostProbe.LEDGER_DURABILITY.value)
    return ModelHostReadingOverlay.model_validate(data)


def settings(**changes: object) -> ModelDurabilityProbeSettings:
    found = durability_overlay(**changes).durability
    assert found is not None
    return found


class Clock:
    """A clock that only moves when the probe sleeps."""

    def __init__(self, start: datetime = RUN_AT) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class LateStore(InMemoryLedger):
    """The durable store holds an appended row only after ``lag`` seconds on the clock."""

    def __init__(self, clock: Clock, lag: int) -> None:
        super().__init__()
        self._clock = clock
        self._lag = lag
        self._visible_at: dict[str, datetime] = {}

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        stamped = "".join(
            f"{line} | req={request_id}\n" for line in rows.splitlines() if line
        )
        self._visible_at[request_id] = self._clock() + timedelta(seconds=self._lag)
        return super().append(stamped, request_id)

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        answer = super().read(query)
        rows = tuple(
            row
            for row in answer.rows
            if self._clock() >= self._visible_at.get(row.cells.get("req", ""), RUN_AT)
        )
        return answer.model_copy(update={"rows": rows})


class AcceptAndDrop:
    """The named break: the append route answers accepted and publishes nothing."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        return ModelAppendOutcome(status="accepted", message=request_id)


class DownReader:
    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error="read-command-exit-1")


def _probe(
    store_appender: ProtocolReadingAppender,
    reader: ProtocolLedgerRowSource,
    clock: Clock,
    **changes: object,
) -> ModelProbeReading:
    return read_durability(
        settings(**changes),
        host=LEDGER_HOST,
        actor=ACTOR,
        appender=store_appender,
        reader=reader,
        clock=clock,
        sleep=clock.sleep,
    )


# -- the in-memory backend --------------------------------------------------------------------


def test_row_read_back_inside_the_deadline_is_working_with_its_lag() -> None:
    clock = Clock()
    store = LateStore(clock, lag=20)
    reading = _probe(store, store, clock)
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.outcome is EnumReadingOutcome.READ
    assert reading.reasons == ("durable-row-read-back",)
    assert reading.cells["durable"] == "yes"
    assert reading.cells["append"] == "accepted"
    assert reading.cells["drill"] == "no"
    # Polled at 0 and 15 (not yet), found at 30: the lag is from the stamp to the read that found it.
    assert reading.cells["durable_lag_s"] == "30"
    assert reading.cells["reads"] == "3"
    assert len(store.lines) == 1
    assert "lane=ledger-durability-probe" in store.lines[0]
    assert "src=durability-probe" in store.lines[0]


def test_named_break_accepted_append_with_no_durable_row_is_not_working() -> None:
    clock = Clock()
    store = InMemoryLedger()
    reading = _probe(AcceptAndDrop(), store, clock)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.outcome is EnumReadingOutcome.READ
    assert reading.reasons == ("no-durable-row-by-deadline",)
    assert reading.cells["append"] == "accepted"
    assert reading.cells["durable"] == "no"
    # It waited the whole deadline, in poll steps, and read at the end.
    assert sum(clock.slept) == 180
    assert reading.cells["reads"] == str(len(clock.slept) + 1)


def test_a_row_later_than_the_deadline_reads_not_working() -> None:
    clock = Clock()
    store = LateStore(clock, lag=181)
    reading = _probe(store, store, clock)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("no-durable-row-by-deadline",)


class SlowStore(LateStore):
    """A read that takes ``read_seconds`` on the clock, as the lab read node can under load."""

    def __init__(self, clock: Clock, lag: int, read_seconds: int) -> None:
        super().__init__(clock, lag)
        self._read_seconds = read_seconds

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self._clock.now += timedelta(seconds=self._read_seconds)
        return super().read(query)


def test_a_row_found_by_a_read_that_ends_past_the_deadline_reads_not_working() -> None:
    # Read on h200 2026-11-06T13:53:35Z: a row found by a read that started before the deadline
    # and ended after it read WORKING with durable_lag_s=232 against deadline_s=180.
    clock = Clock()
    store = SlowStore(clock, lag=170, read_seconds=40)
    reading = _probe(store, store, clock)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING, reading
    assert reading.reasons == ("durable-row-after-deadline",)
    assert reading.cells["durable"] == "late"
    assert int(reading.cells["durable_lag_s"]) > 180


def test_a_row_found_by_a_slow_read_inside_the_deadline_is_working() -> None:
    clock = Clock()
    store = SlowStore(clock, lag=20, read_seconds=40)
    reading = _probe(store, store, clock)
    assert reading.verdict is EnumReadingVerdict.WORKING, reading
    # Found by the first read, which ended at 40 s: the lag runs to the end of that read.
    assert reading.cells["durable_lag_s"] == "40"


@pytest.mark.parametrize("status", ["refused", "pending", "error"])
def test_append_that_does_not_land_is_not_working_and_reads_nothing(
    status: str,
) -> None:
    clock = Clock()
    store = InMemoryLedger()
    store.append_status = status
    reading = _probe(store, DownReader(), clock)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == (f"probe-append-{status}",)
    assert reading.cells["reads"] == "0"
    assert clock.slept == []


def test_read_route_down_is_unknown_never_working() -> None:
    clock = Clock()
    store = InMemoryLedger()
    reading = _probe(store, DownReader(), clock)
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert reading.reasons == ("durable-read-failed-read-command-exit-1",)
    assert reading.cells["answered"] == "0"


def test_drill_overlay_marks_the_probe_and_the_reading() -> None:
    clock = Clock()
    store = LateStore(clock, lag=0)
    reading = _probe(store, store, clock, drill=True)
    assert reading.cells["drill"] == "yes"
    assert "drill=yes" in store.lines[0]


def test_a_host_declaring_the_probe_without_settings_does_not_load() -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["durability"] = None
    data["hosts"][0]["probes"].append(EnumHostProbe.LEDGER_DURABILITY.value)
    with pytest.raises(ValueError, match="no durability settings"):
        ModelHostReadingOverlay.model_validate(data)


def test_handler_without_the_durability_routes_reads_unknown(tmp_path: Path) -> None:
    world = World("memory", tmp_path)
    handler = HandlerHostReading(
        overlay=durability_overlay(),
        host=LEDGER_HOST,
        files=world.files,
        appender=None,
        clock=lambda: RUN_AT,
        environ=world.environ,
    )
    result = handler.handle(
        ModelHostReadingCommand(
            correlation_id="c",
            probes=(EnumHostProbe.LEDGER_DURABILITY,),
            write=False,
        )
    )
    assert result.probes[0].verdict is EnumReadingVerdict.UNKNOWN
    assert result.probes[0].reasons == ("durability-routes-not-wired",)


def test_handler_writes_the_reading_row_with_the_measured_cells(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    clock = Clock()
    store = LateStore(clock, lag=5)
    readings = InMemoryLedger()
    handler = HandlerHostReading(
        overlay=durability_overlay(),
        host=LEDGER_HOST,
        files=world.files,
        appender=readings,
        clock=clock,
        environ=world.environ,
        probe_appender=store,
        durable_reader=store,
        sleep=clock.sleep,
    )
    result = handler.handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.LEDGER_DURABILITY,)
        )
    )
    assert result.probes[0].ledger == "written"
    (row,) = readings.lines
    assert "probe=ledger-durability" in row
    assert "verdict=WORKING" in row
    assert "durable=yes" in row
    assert "durable_lag_s=15" in row
    assert "drill=no" in row
    assert "injected fault: An append route that answers accepted" in row


# -- the command backend: the real adapters against two small scripts ---------------------------

_APPEND = """
import json, sys
argv = sys.argv[1:]
request = argv[argv.index("--request-id") + 1]
rows = sys.stdin.read()
with open(sys.argv[1], "a", encoding="utf-8") as store:
    store.write(rows)
print(json.dumps({"status": "accepted", "request_id": request}))
"""

_READ = """
import json, sys
query = json.loads(sys.stdin.read())
rows = []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.rstrip("\\n")
    if not line or query.get("term", "") not in line:
        continue
    rows.append({"raw_row": line, "projected_at": sys.argv[2]})
print(json.dumps({"status": "ok", "rows": rows, "matched": len(rows)}))
"""


def _scripts(tmp_path: Path, projected_at: str) -> tuple[list[str], list[str]]:
    store = tmp_path / "durable.md"
    store.write_text("", encoding="utf-8")
    append = tmp_path / "append.py"
    append.write_text(_APPEND, encoding="utf-8")
    read = tmp_path / "read.py"
    read.write_text(_READ, encoding="utf-8")
    return (
        [sys.executable, str(append), str(store)],
        [sys.executable, str(read), str(store), projected_at],
    )


def test_command_backend_reads_back_with_the_projected_lag(tmp_path: Path) -> None:
    clock = Clock()
    projected = (RUN_AT + timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    append_argv, read_argv = _scripts(tmp_path, projected)
    reading = _probe(
        CommandLedgerAppender(append_argv, "ledger-durability-probe", LEDGER_HOST, 30),
        CommandLedgerRowSource(read_argv, 30, 10),
        clock,
    )
    assert reading.verdict is EnumReadingVerdict.WORKING, reading
    assert reading.cells["durable_lag_s"] == "7"


def test_command_backend_named_break_is_not_working(tmp_path: Path) -> None:
    clock = Clock()
    _, read_argv = _scripts(tmp_path, RUN_AT.strftime("%Y-%m-%dT%H:%M:%SZ"))
    drop = tmp_path / "drop.py"
    drop.write_text('print(\'{"status": "accepted"}\')\n', encoding="utf-8")
    reading = _probe(
        CommandLedgerAppender(
            [sys.executable, str(drop)], "ledger-durability-probe", LEDGER_HOST, 30
        ),
        CommandLedgerRowSource(read_argv, 30, 10),
        clock,
        deadline_seconds=30,
    )
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == ("no-durable-row-by-deadline",)


def test_command_backend_failing_read_command_is_unknown(tmp_path: Path) -> None:
    clock = Clock()
    append_argv, _ = _scripts(tmp_path, RUN_AT.strftime("%Y-%m-%dT%H:%M:%SZ"))
    reading = _probe(
        CommandLedgerAppender(append_argv, "ledger-durability-probe", LEDGER_HOST, 30),
        CommandLedgerRowSource([sys.executable, "-c", "raise SystemExit(1)"], 30, 10),
        clock,
        deadline_seconds=30,
    )
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.reasons == ("durable-read-failed-read-command-exit-1",)
