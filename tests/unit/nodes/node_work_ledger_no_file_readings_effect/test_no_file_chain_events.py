# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the seven no-file readings (OMN-22917, piece 6).

Golden: a start, then a success under one correlation id, and with write one STATUS row per reading
that passes the ledger row grammar. Error: no appender for a writing run, an append that does not land,
a probe that cannot run (UNKNOWN, never WORKING) and a command route with no probe wired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.adapters import (
    CommandNoFileFacts,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelNoFileRequest,
    ModelNoFileRoute,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.runtime_no_file_readings import (
    main,
)
from tests.unit.nodes.node_work_ledger_reader_effect.no_file_world import (
    NOW,
    RecordingAppender,
    clean_world,
    overlay,
    readings,
    world_source,
)

pytestmark = pytest.mark.unit
NODE = "node_work_ledger_no_file_readings_effect"


def phases(sink: InMemoryEmissionSink) -> list[tuple[str, str | None]]:
    return [(e.phase, e.failure_code) for e in sink.events]


def test_golden_chain_writes_seven_rows_and_ends_in_success() -> None:
    appender = RecordingAppender()
    node, sink = readings(world_source(clean_world()), appender=appender)
    result = node.handle(ModelNoFileRequest(correlation_id="golden", write=True))
    assert result.status == "completed"
    assert phases(sink) == [("start", None), ("success", None)]
    assert {(e.node, e.piece, e.correlation_id) for e in sink.events} == {
        (NODE, 6, "golden")
    }
    assert sink.events[-1].counters == {"readings": 7, "working": 7}
    assert len(appender.rows) == 7
    assert {r.ledger for r in result.readings} == {"written"}
    first = appender.rows[0]
    assert first.startswith(
        "2026-11-07T15:30:00Z | STATUS | lane=work-ledger-no-file | "
    )
    assert "src=no-file-readings" in first
    assert "verdict=WORKING" in first
    assert "injected fault: re-create a file at the ledger path" in first
    for row in appender.rows:
        parsed = parse_row(row.strip())
        assert parsed is not None
        assert parsed.row_type == "STATUS"


def test_a_red_reading_is_still_a_completed_chain_and_a_written_row() -> None:
    world = clean_world().model_copy(
        update={
            "publish": clean_world().publish.model_copy(update={"job_listed": True})
        }
    )
    appender = RecordingAppender()
    node, sink = readings(world_source(world), appender=appender)
    result = node.handle(ModelNoFileRequest(correlation_id="red", write=True))
    assert phases(sink) == [("start", None), ("success", None)]
    assert not result.all_working
    assert "verdict=NOT_WORKING" in appender.rows[5]


def test_error_chain_a_writing_run_without_an_appender() -> None:
    node, sink = readings(world_source(clean_world()))
    result = node.handle(ModelNoFileRequest(correlation_id="e1", write=True))
    assert result.status == "failed"
    assert phases(sink) == [
        ("start", None),
        ("failure", "no-appender-for-a-writing-run"),
    ]


def test_error_chain_an_append_that_does_not_land() -> None:
    appender = RecordingAppender(status="refused")
    node, sink = readings(world_source(clean_world()), appender=appender)
    result = node.handle(ModelNoFileRequest(correlation_id="e2", write=True))
    assert result.status == "failed"
    assert phases(sink)[-1] == ("failure", "append-failed")
    assert "reading 1" in result.failure_detail


class DeadProbe:
    """Every probe raises, as a host that cannot be reached does."""

    def _dead(self) -> NoReturn:
        raise OSError("probe host unreachable")

    def file_probe(self) -> NoReturn:
        self._dead()

    def census(self) -> NoReturn:
        self._dead()

    def absence(self) -> NoReturn:
        self._dead()

    def rebuild(self) -> NoReturn:
        self._dead()

    def restore(self) -> NoReturn:
        self._dead()

    def publish(self) -> NoReturn:
        self._dead()

    def receipt(self) -> NoReturn:
        self._dead()


def test_error_chain_unrunnable_probes_read_unknown_never_working() -> None:
    node, sink = readings(DeadProbe())
    result = node.handle(ModelNoFileRequest(correlation_id="e3"))
    assert {r.verdict for r in result.readings} == {EnumReadingVerdict.UNKNOWN}
    assert not result.all_working
    assert phases(sink) == [("start", None), ("success", None)]


def test_command_backend_parses_the_probe_output_and_refuses_an_unwired_probe() -> None:
    route = ModelNoFileRoute(
        kind="command",
        command_envs={"restore": "PROBE_RESTORE", "census": "PROBE_CENSUS"},
    )
    output = json.dumps(
        {"ran": True, "snapshot_rows": 4, "restored_rows": 4, "checksum_ok": True}
    )
    facts = CommandNoFileFacts(
        route,
        {"PROBE_RESTORE": "probe restore --now"},
        runner=lambda argv, _timeout: (
            output if argv[:2] == ["probe", "restore"] else ""
        ),
    )
    assert facts.restore().restored_rows == 4
    with pytest.raises(OSError, match="probe-not-wired-census"):
        facts.census()
    with pytest.raises(OSError, match="probe-not-wired-publish"):
        facts.publish()


def test_command_backend_bad_json_is_a_value_error_and_reads_unknown() -> None:
    route = ModelNoFileRoute(kind="command", command_envs={"restore": "PROBE_RESTORE"})
    facts = CommandNoFileFacts(
        route, {"PROBE_RESTORE": "x"}, runner=lambda _argv, _timeout: "not json"
    )
    node, _ = readings(facts)
    result = node.handle(ModelNoFileRequest(correlation_id="bad-json"))
    assert result.readings[4].verdict is EnumReadingVerdict.UNKNOWN


def test_cli_prints_seven_lines_and_exits_one_when_a_reading_is_red(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import yaml

    from tests.unit.nodes.node_work_ledger_reader_effect.no_file_world import PACKAGE

    path = tmp_path / "overlay.yaml"
    path.write_text((PACKAGE / "overlay.example.yaml").read_text())
    env = {"ONEX_LEDGER_NO_FILE_OVERLAY": str(path)}
    assert main([], environ=env, facts=world_source(clean_world())) == 0
    red = clean_world().model_copy(
        update={"census": clean_world().census.model_copy(update={"total_files": 3})}
    )
    assert main([], environ=env, facts=world_source(red)) == 1
    lines = capsys.readouterr().out.splitlines()
    assert sum(line.startswith("READING ") for line in lines) == 14
    assert any(line.startswith("READING 2 NOT_WORKING") for line in lines)
    assert overlay().route.kind == "inmemory"
    assert yaml.safe_load(path.read_text())["census_total_bound"] == 0
    assert NOW.year == 2026
