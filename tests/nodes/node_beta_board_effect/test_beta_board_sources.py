# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: the readers. A probe's finished runs and ledger rows become dated verdicts; a reader that fails says unread."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_beta_board_effect.handlers.adapter_beta_board_sources import (
    read_ledger_readings,
    read_probe,
    refresh_probes,
    remote_write_command,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerRead
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    ago,
    overlay,
    run_list_answer,
    runtime,
)


def _scripted(code: int, out: str, err: str):
    def run(_argv, _timeout):
        return code, out, err

    return run


def probe(pid: str):
    return next(p for p in overlay().probes if p.id == pid)


def test_beta_board_probe_reads_only_finished_success_and_failure_runs_newest_first() -> (
    None
):
    answer = run_list_answer(
        [
            ("in_progress", "", ago(minutes=1)),
            ("completed", "cancelled", ago(minutes=10)),
            ("completed", "failure", ago(minutes=20)),
            ("completed", "success", ago(minutes=80)),
        ]
    )
    seen: list[list[str]] = []

    def run(argv, timeout):
        seen.append(list(argv))
        return 0, answer + "\n", ""

    got = read_probe(probe("probe-local"), runtime(), Path("/x/onex"), run, NOW)
    assert got.read == "read"
    assert [p.verdict for p in got.points] == ["fail", "pass"]
    assert seen[0][:5] == ["/x/onex", "scm", "run-list", "Org/localrepo", "--workflow"]
    assert seen[0][5] == "local.yml"


def test_beta_board_probe_unreadable_answer_is_unread_with_the_reason() -> None:
    got = read_probe(
        probe("probe-local"),
        runtime(),
        Path("/x/onex"),
        _scripted(1, "", "boom: no gateway"),
        NOW,
    )
    assert got.read == "unread"
    assert "no gateway" in got.reason


def test_beta_board_probe_failed_outcome_is_unread() -> None:
    answer = '{"outcome":"failed","result":{"failure_reason":"HTTP 404 not found"}}'
    got = read_probe(
        probe("probe-local"),
        runtime(),
        Path("/x/onex"),
        _scripted(0, answer, ""),
        NOW,
    )
    assert got.read == "unread"
    assert "404" in got.reason


LEDGER = [
    "2026-11-09T09:00:00Z | STATUS | lane=a | src=thing | verdict=NOT_WORKING | probed_at=2026-11-09T09:00:00Z",
    "2026-11-09T10:00:00Z | STATUS | lane=a | src=other | verdict=NOT_WORKING | probed_at=2026-11-09T10:00:00Z",
    "2026-11-09T11:00:00Z | STATUS | lane=a | src=thing | verdict=WORKING | probed_at=2026-11-09T10:59:00Z",
    "2026-11-09T11:30:00Z | TERMINAL | lane=a | src=thing | verdict=NOT_WORKING",
    "2026-11-09T11:40:00Z | STATUS | lane=a | src=thing | verdict=UNKNOWN | probed_at=2026-11-09T11:40:00Z",
    "not a ledger row",
]


def test_beta_board_ledger_reading_selects_by_type_and_cells_and_classifies_the_verdict_word() -> (
    None
):
    rt = runtime()
    got = read_ledger_readings(
        InMemoryLedger(LEDGER), rt.overlay.ledger_readings, rt, NOW
    )["ledger-one"]
    assert got.read == "read"
    assert [p.verdict for p in got.points] == ["unknown", "pass", "fail"]
    assert (
        got.points[1].at.isoformat() == "2026-11-09T10:59:00+00:00"
    )  # the time cell, not the row stamp


def test_beta_board_ledger_route_that_cannot_be_read_leaves_the_reading_unread() -> (
    None
):
    class Broken:
        def read(self, query):
            return ModelLedgerRead(error="read-command-exit-3")

    rt = runtime()
    got = read_ledger_readings(Broken(), rt.overlay.ledger_readings, rt, NOW)
    assert got["ledger-one"].read == "unread"
    assert "read-command-exit-3" in got["ledger-one"].reason


def test_beta_board_ledger_reading_with_no_matching_row_reads_with_no_points() -> None:
    rt = runtime()
    got = read_ledger_readings(
        InMemoryLedger([LEDGER[1]]), rt.overlay.ledger_readings, rt, NOW
    )["ledger-one"]
    assert got.read == "read"
    assert got.points == ()


def test_beta_board_refresh_keeps_a_good_earlier_read_when_the_new_read_fails_and_stops_at_the_deadline(
    tmp_path: Path,
) -> None:
    rt = runtime()
    good = run_list_answer([("completed", "success", ago(minutes=5))])
    calls: list[str] = []

    def run_ok(argv, timeout):
        calls.append(argv[3])
        return 0, good, ""

    first = refresh_probes(rt, tmp_path, NOW, run_ok, lambda: NOW)
    assert {r.source_id for r in first.reads} == {"probe-local", "probe-cloud"}
    (tmp_path / rt.reads_relpath).parent.mkdir(parents=True)
    (tmp_path / rt.reads_relpath).write_text(first.model_dump_json())
    later = NOW.replace(hour=13)
    second = refresh_probes(
        rt, tmp_path, later, _scripted(1, "", "down"), lambda: later
    )
    assert all(
        r.read == "read" for r in second.reads
    )  # the failed read did not erase the good one
    refresh_probes(rt, tmp_path, NOW, run_ok, lambda: NOW)
    assert len(calls) == 2  # read within probe_refresh_seconds: not read again


def test_beta_board_remote_write_is_a_temporary_name_renamed_into_place() -> None:
    cmd = remote_write_command("/srv/a b", "beta.html")
    assert "'/srv/a b/.beta.html.tmp'" in cmd
    assert "mv -f" in cmd
    assert "rm -f" in cmd
