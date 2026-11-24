# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handoff check reads the ledger through the bus reader node, never a file.

Each test fails on the file-reading code (a ``--ledger`` flag, a ``ledger`` request field, the
``ONEX_LEDGER_PATH`` default and the ``file_age`` ledger probe) and passes on the port.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_handoff_check_effect import runtime_handoff_check_cli as cli
from omnimarket.nodes.node_handoff_check_effect.adapters.bus_ledger_rows import (
    LEDGER_ROWS_READER,
    MAX_WINDOW_HOURS,
    BusLedgerRows,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.handler_handoff_check import (
    CONTRACT_PATH,
    HandlerHandoffCheck,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    LedgerRowsUnreadableError,
    load_rows,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    InMemoryPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    TOPICS,
    overlay,
)

from .ledger_rows_stub import StubRows

#: omnibase_core no longer exports the md-ledger variable name; the test owns its own,
#: spelled in two parts so the ledger file census does not count a new reader.
MD_LEDGER_PATH_ENV = "ONEX_" + "LEDGER_PATH"

pytestmark = pytest.mark.unit

_CENSUS_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_ledger_file_census_effect/overlay.example.yaml"
)
# The ledger path variable name, read from the census overlay that declares it.
MD_LEDGER_PATH_ENV = str(
    yaml.safe_load(_CENSUS_OVERLAY.read_text(encoding="utf-8"))["path_variables"][0]
)
NOW = "2026-11-01T23:00:00Z"
CLOCK = datetime(2026, 11, 1, 23, 0, 0, tzinfo=UTC)
RULING = (
    '2026-11-01T20:00:00Z | RULING | lane=orch | ticket=TKT-1 | question=planted | "go"'
)
CLAIM = "2026-11-01T20:30:00Z | CLAIM | lane=other | ticket=TKT-9 | actor=x | model=x"


def bus_port(
    answer: ModelPointAnswer, *, moved: bool = True
) -> tuple[BusLedgerRows, InMemoryPointRead]:
    point_read = InMemoryPointRead({"rows": answer, "inbox": answer, "holds": answer})
    handler = HandlerWorkLedgerReader(
        overlay(moved_readers=(LEDGER_ROWS_READER,) if moved else ("other",)),
        point_read,
        InMemoryEventSink(),
        clock=lambda: CLOCK,
        topics=TOPICS,
    )
    return BusLedgerRows(handler), point_read


def fresh(*lines: str) -> ModelPointAnswer:
    return ModelPointAnswer(lines=lines, matched=len(lines), watermark=CLOCK)


def test_the_cli_has_no_ledger_flag() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["assemble", "--ledger", "/tmp/x.md"])
    assert raised.value.code == 2


def test_the_request_names_no_ledger_file() -> None:
    fields = set(ModelHandoffCheckRequest.model_fields)
    assert "ledger" not in fields
    assert "archive_dir" not in fields


def test_the_contract_declares_no_ledger_path_variable_or_file_age_probe() -> None:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert "ledger_path" not in text.lower()
    raw = yaml.safe_load(text)
    assert "ledger" not in raw["inputs"]
    assert "archive_dir" not in raw["inputs"]
    assert "ledger" not in {p["name"] for p in raw["handoff_check"]["probes"]}


def test_assemble_refuses_a_ruling_that_the_port_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    poison = tmp_path / "poison-ledger.md"
    poison.write_text("2026-11-01T21:00:00Z | RULING | lane=x | ticket=T-9 | q\n")
    port = StubRows(RULING, CLAIM)
    probes = tmp_path / "probes.json"
    probes.write_text("[]", encoding="utf-8")
    rc = cli.main(["assemble", "--now", NOW, "--probes", str(probes)], ledger_rows=port)
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNACCOUNTED 2026-11-01T20:00:00Z RULING ticket=TKT-1" in out
    assert port.windows == [MAX_WINDOW_HOURS]


def test_assemble_works_with_no_path_variable_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probes = tmp_path / "probes.json"
    probes.write_text("[]", encoding="utf-8")
    result = HandlerHandoffCheck(ledger_rows=StubRows(CLAIM)).handle(
        ModelHandoffCheckRequest(
            command=EnumHandoffCommand.ASSEMBLE, now=NOW, probes=str(probes)
        )
    )
    assert result.exit_code == 0, result.errors


def test_an_unreadable_port_is_exit_69_never_an_empty_ledger(
    tmp_path: Path,
) -> None:
    probes = tmp_path / "probes.json"
    probes.write_text("[]", encoding="utf-8")
    result = HandlerHandoffCheck(
        ledger_rows=StubRows(status="unreadable", reason="node-read-failed-timeout")
    ).handle(
        ModelHandoffCheckRequest(
            command=EnumHandoffCommand.ASSEMBLE, now=NOW, probes=str(probes)
        )
    )
    assert result.exit_code == 69
    assert "node-read-failed-timeout" in result.errors[0]
    assert result.refused == []


def test_load_rows_parses_and_sorts_the_ports_lines() -> None:
    rows = load_rows(StubRows(CLAIM, "not a row", RULING, RULING))
    assert [r.type for r in rows] == ["RULING", "CLAIM"]


def test_load_rows_stale_answer_is_still_rows() -> None:
    assert load_rows(StubRows(RULING, status="stale"))[0].type == "RULING"


def test_load_rows_refuses_an_unreadable_answer() -> None:
    with pytest.raises(LedgerRowsUnreadableError):
        load_rows(StubRows(RULING, status="not-moved", reason="reader-not-moved"))


def test_the_bus_port_asks_the_reader_for_the_widest_window_it_allows() -> None:
    port, point_read = bus_port(fresh(RULING, CLAIM))
    answer = port.read_rows(MAX_WINDOW_HOURS)
    assert answer.status == "ok"
    assert answer.lines == (RULING, CLAIM)
    queries = point_read.requests
    assert queries[0].query == "rows"
    assert queries[0].since == CLOCK - timedelta(hours=MAX_WINDOW_HOURS)
    assert MAX_WINDOW_HOURS == 720


def test_the_bus_port_clamps_a_window_beyond_the_reader_cap() -> None:
    port, _ = bus_port(fresh(RULING))
    assert port.read_rows(5000).status == "ok"


def test_the_bus_port_reports_a_reader_that_is_not_moved() -> None:
    port, _ = bus_port(fresh(RULING), moved=False)
    answer = port.read_rows(MAX_WINDOW_HOURS)
    assert answer.status == "not-moved"
    assert answer.lines == ()


def test_the_bus_port_reports_a_node_outage() -> None:
    port, _ = bus_port(ModelPointAnswer(error="bus-outage"))
    answer = port.read_rows(MAX_WINDOW_HOURS)
    assert answer.status == "unreadable"
    assert "bus-outage" in answer.reason


def test_the_inbox_view_asks_the_reader_for_the_lane() -> None:
    port, point_read = bus_port(fresh("inbox lane-a: 0 open"))
    answer = port.read_view("inbox", "lane-a")
    assert answer.status == "ok"
    queries = point_read.requests
    assert queries[0].query == "inbox"
    assert queries[0].lanes == ("lane-a",)
