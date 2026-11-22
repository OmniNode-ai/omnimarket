# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor_simple cycle reads the ledger over the bus read command and stays inside its step limit (OMN-23081, OMN-26329).

The publisher gives ``floor_simple`` 15 s. The cycle's ledger route was the live file and its rolls (``FileLedgerRowSource``,
measured on 2026-11-08 at 14.1 to 14.6 s on the launching Mac, almost all of it the file parse); it is now the bus read command
the contract's ``runtime.ledger`` names, the route ``CommandLedgerRowSource`` serves (OMN-26329). On this route the floor's cost
is the read command: one command run per query, so these tests hold what the route owes the step limit: the contract names the
command route and no file variable, one command runs per query and the number of queries per cycle stays under a bound, the
facts equal the facts of the in-memory ledger over the same rows (a positive control: they are not empty), the cycle over a
ledger the size of the one the publisher read finishes under a bound with a command that answers at once, and a command that
fails is an unread ledger, never an empty one.

The ledger here holds the recorded rows of the fixtures among generated rows spread over the 28 days before the cycle; the
stub command answers the typed query the floor sends it, filtering by window, kind, lane and term as the read node does.
"""

from __future__ import annotations

import shlex
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
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
from tests.nodes.node_floor_simple_effect.support import (
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)

CONTRACT_LEDGER: dict[str, Any] = yaml.safe_load(
    (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_floor_simple_effect/contract.yaml"
    ).read_text(encoding="utf-8")
)["runtime"]["ledger"]
# The variable the contract names for the read command; it is taken from the contract rather than spelled here.
CONTRACT_COMMAND_ENV: str = CONTRACT_LEDGER["command_env"]
# Rows in the stub ledger: the recorded rows among generated ones. The read command filters in its store, so the rows the
# floor receives are the recorded ones whatever the size; the generated rows keep the stub's own filter honest.
LEDGER_ROWS = 7_000
# The step limit is 15 s. The bound is a third of it: with a command that answers at once the cycle is the floor's own work.
CYCLE_BOUND_S = 5.0
# The floor runs one command per query. A real read answers in about 5.5 to 6.5 s (measured on the dev lane 2026-11-11), so
# the number of commands per cycle, not the file size, sets the step time on this route: this is the bound the count must stay under.
COMMANDS_PER_CYCLE_BOUND = 40


def event_key(e: Any) -> tuple[Any, Any, Any]:
    return (e.emitted_at, e.correlation_id, e.phase)


FILLER_TYPES = ("STATUS", "CLAIM", "TERMINAL", "MSG", "FRICTION", "ACK")


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def recorded_rows() -> list[str]:
    rows: list[str] = []
    for name in (
        "ledger_rows_go_and_code_graph.txt",
        "ledger_rows_piece_events.txt",
        "ledger_rows_reading_writer.txt",
    ):
        rows.extend(
            x
            for x in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
            if x.strip()
        )
    return rows


def filler_row(index: int) -> str:
    """One row of the size and shape of a lane's STATUS or CLAIM row, spread over the 28 days before ``AT``."""
    stamp = (AT - timedelta(seconds=(index * 19) % (28 * 86400))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kind = FILLER_TYPES[index % len(FILLER_TYPES)]
    return (
        f"{stamp} | {kind} | lane=filler-{index % 400} | ticket=OMN-{20000 + index % 900} | "
        f"actor=claude:sonnet:subagent | model=sonnet | band=B3 | repo=omnimarket | "
        f"worktree=omni_worktrees/OMN-{20000 + index % 900}/omnimarket | pr=omnimarket#{index % 700} | "
        f"scope=filler row {index} of the generated ledger, long enough to look like a lane row with its prose | "
        f"est ~1 lane-hours; displaces nothing; (OMN-{20000 + index % 900})"
    )


def ledger_lines(total: int = LEDGER_ROWS) -> list[str]:
    """``total`` generated rows with the recorded rows among them."""
    return [*(filler_row(index) for index in range(total)), *recorded_rows()]


# The stub read command: it answers one typed query on stdin the way the work ledger read node does, from the rows file the
# environment names, and appends the query to the log file the environment names.
READ_STUB = """\
import json, os, sys
from datetime import datetime
request = json.loads(sys.stdin.read())
rows_file, log_file, exit_code = sys.argv[1], sys.argv[2], int(sys.argv[3])
if exit_code:
    sys.stderr.write("Work-ledger read refused (StubRefusal)")
    sys.exit(exit_code)
with open(log_file, "a", encoding="utf-8") as log:
    log.write(json.dumps({key: request.get(key) for key in ("kinds", "lane", "term", "limit")}) + "\\n")
def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
since, until = stamp(request["since"]), stamp(request["until"])
kinds = request.get("kinds") or []
lane = request.get("lane")
term = (request.get("term") or "").lower()
rows = []
for line in open(rows_file, encoding="utf-8").read().splitlines():
    cells = [cell.strip() for cell in line.split(" | ")]
    if len(cells) < 3 or len(cells[0]) != 20:
        continue
    if not (since <= stamp(cells[0]) < until) or (kinds and cells[1] not in kinds):
        continue
    if lane is not None and "lane=" + lane not in cells:
        continue
    if term and term not in line.lower():
        continue
    rows.append({"raw_row": line, "projected_at": cells[0]})
limit = int(request.get("limit") or len(rows))
print(json.dumps({"status": "ok", "rows": rows[:limit], "matched": len(rows)}))
"""


class StubBus:
    """The stub read command, its rows and its query log, in one directory."""

    def __init__(self, directory: Path, lines: list[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "bus_read_stub.py"
        script.write_text(READ_STUB, encoding="utf-8")
        self.rows_file = directory / "rows.md"
        self.rows_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log = directory / "queries.jsonl"
        self.log.write_text("", encoding="utf-8")
        self.script = script
        self.lines = lines

    def command(self, exit_code: int = 0) -> str:
        """The read command line: the stub, its rows file, its query log and the exit code it answers with."""
        return shlex.join(
            [
                sys.executable,
                str(self.script),
                str(self.rows_file),
                str(self.log),
                str(exit_code),
            ]
        )

    def environment(self, exit_code: int = 0) -> dict[str, str]:
        return {CONTRACT_COMMAND_ENV: self.command(exit_code)}

    def commands_run(self) -> int:
        return len(self.log.read_text(encoding="utf-8").splitlines())


@pytest.fixture(scope="module")
def stub_bus(tmp_path_factory: pytest.TempPathFactory) -> StubBus:
    return StubBus(tmp_path_factory.mktemp("bus"), ledger_lines())


def route() -> ModelCommandLedgerReadRoute:
    return ModelCommandLedgerReadRoute.model_validate(CONTRACT_LEDGER)


def command_source(bus: StubBus, exit_code: int = 0) -> CommandLedgerRowSource:
    source = build_row_source(route(), bus.environment(exit_code))
    assert isinstance(source, CommandLedgerRowSource)
    return source


def test_the_contract_names_the_read_command_route_and_no_ledger_file() -> None:
    assert CONTRACT_LEDGER["route"] == "command"
    assert CONTRACT_LEDGER["command_env"] == "ONEX_LEDGER_BUS_READ_COMMAND"
    assert set(CONTRACT_LEDGER) == {
        "route",
        "command_env",
        "timeout_seconds",
        "row_limit",
    }
    assert route().timeout_seconds == 60


def test_one_command_runs_per_query_and_the_count_stays_under_the_bound(
    stub_bus: StubBus,
) -> None:
    queries: list[ModelLedgerQuery] = []
    inner = command_source(stub_bus)

    class Recording:
        def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
            queries.append(query)
            return inner.read(query)

    before = stub_bus.commands_run()
    result = read_ledger_facts(Recording(), overlay(), AT)
    assert result.error is None
    assert result.facts is not None
    commands = stub_bus.commands_run() - before
    assert commands == len(queries)
    assert 0 < commands <= COMMANDS_PER_CYCLE_BOUND, commands


def test_the_facts_over_the_command_route_equal_the_facts_of_the_in_memory_ledger(
    stub_bus: StubBus,
) -> None:
    over_command = read_ledger_facts(command_source(stub_bus), overlay(), AT)
    in_memory = read_ledger_facts(InMemoryLedger(stub_bus.lines), overlay(), AT)
    assert over_command.facts is not None
    assert in_memory.facts is not None
    once, other = over_command.facts, in_memory.facts
    assert once.go_given_through == other.go_given_through
    assert once.go_rows == other.go_rows
    assert once.readings == other.readings
    assert once.readings_rejected == other.readings_rejected
    assert once.shadows == other.shadows
    assert once.shadows_rejected == other.shadows_rejected
    assert once.shadows_unread == other.shadows_unread
    assert sorted(once.events, key=event_key) == sorted(other.events, key=event_key)
    # The recorded rows are read: a parity over empty facts would prove nothing (positive control).
    assert once.events
    assert once.readings
    assert once.go_given_through > 0


def test_a_read_command_that_fails_is_an_unread_ledger_and_never_empty(
    stub_bus: StubBus,
) -> None:
    result = read_ledger_facts(command_source(stub_bus, exit_code=3), overlay(), AT)
    assert result.facts is None
    assert result.error is not None
    assert "go rulings unread" in result.error
    assert "read-command-exit-3:StubRefusal" in result.error


def test_the_whole_cycle_over_the_command_route_finishes_under_the_bound(
    tmp_path: Path, stub_bus: StubBus
) -> None:
    home = build_home(tmp_path)
    env = {**environ(home), **stub_bus.environment()}
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
    )
    before = stub_bus.commands_run()
    started = time.perf_counter()
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=env
    )
    elapsed = time.perf_counter() - started
    assert result.ok
    document = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert document.sources_read["ledger"] is True
    assert (
        stub_bus.commands_run() > before
    )  # the cycle read the ledger through the command
    assert elapsed < CYCLE_BOUND_S, (
        f"the cycle took {elapsed:.1f} s with a read command that answers at once; the bound is {CYCLE_BOUND_S} s "
        "(the publisher's step limit is 15 s)"
    )


def test_the_cycle_with_the_command_unset_reads_the_ledger_as_unread(
    tmp_path: Path, stub_bus: StubBus
) -> None:
    home = build_home(tmp_path)
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
    )
    handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=environ(home)
    )
    document = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert document.sources_read["ledger"] is False
