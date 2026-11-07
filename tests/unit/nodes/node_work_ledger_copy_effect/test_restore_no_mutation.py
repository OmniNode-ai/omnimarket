# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The job and the drill, wired as shipped, issue only commands that cannot stop or restart the source host (OMN-22916).

Every command goes through a recording runner that runs nothing. The reading is the allow list in
``assert_no_mutation``; the named break ``restart_source`` shows it goes red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_copy_effect.container.container_work_ledger_copy_effect import (
    create_handler_work_ledger_restore_drill,
    create_handler_work_ledger_snapshot,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumDrillOutcome,
    ModelDrillResult,
    ModelSnapshotOverlay,
    ModelSnapshotResult,
)
from tests.unit.nodes.node_work_ledger_copy_effect.support_snapshot import (
    RecordingRunner,
    assert_no_mutation,
    drill_request,
    rows,
    snapshot_request,
)

pytestmark = pytest.mark.unit
_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_copy_effect/overlay.snapshot.example.yaml"
)
_ALLOWED = frozenset({"/usr/local/bin/example-export", "nc", "onex-ledger"})


def _overlay(tmp_path: Path) -> ModelSnapshotOverlay:
    raw = yaml.safe_load(_EXAMPLE.read_text())
    raw["store"]["directory"] = str(tmp_path / "snapshots")
    raw["store"]["host"] = "drill.example.invalid"
    raw["drill"]["host"] = "drill.example.invalid"
    return ModelSnapshotOverlay.model_validate(raw)


def _export() -> str:
    return "".join(row.model_dump_json() + "\n" for row in rows(7))


def _runner(*, nc_exit: int = 1) -> RecordingRunner:
    return RecordingRunner(
        {
            "/usr/local/bin/example-export": (0, _export()),
            "nc": (nc_exit, ""),
            "onex-ledger": (0, ""),
        }
    )


def _job_and_drill(
    tmp_path: Path, runner: RecordingRunner
) -> tuple[ModelSnapshotOverlay, ModelSnapshotResult, ModelDrillResult]:
    overlay = _overlay(tmp_path)
    made = create_handler_work_ledger_snapshot(overlay, {}, run=runner).handle(
        snapshot_request()
    )
    drilled = create_handler_work_ledger_restore_drill(overlay, {}, run=runner).handle(
        drill_request(None)
    )
    return overlay, made, drilled


def test_job_and_drill_pass_and_issue_only_allowed_commands(tmp_path: Path) -> None:
    runner = _runner()
    overlay, made, drilled = _job_and_drill(tmp_path, runner)
    assert made.ok
    assert drilled.outcome is EnumDrillOutcome.PASSED
    assert drilled.rebuilt_rows == 7
    assert_no_mutation(runner.commands, overlay.source.host, _ALLOWED)
    verbs = [argv[0] for argv in runner.commands]
    assert verbs.count("/usr/local/bin/example-export") == 2
    assert verbs.count("nc") == 1
    probe = next(argv for argv in runner.commands if argv[0] == "nc")
    assert probe == (
        "nc",
        "-z",
        "-w",
        "3",
        overlay.source.host,
        str(overlay.source.port),
    )
    row = next(argv for argv in runner.commands if argv[0] == "onex-ledger")[-1]
    assert "| STATUS |" in row
    assert "outcome=drill-passed" in row
    assert "ticket=OMN-1 " in row.replace("|", "")


def test_a_reachable_source_stops_the_drill_after_its_one_probe(tmp_path: Path) -> None:
    runner = _runner(nc_exit=0)
    overlay, _, drilled = _job_and_drill(tmp_path, runner)
    assert drilled.outcome is EnumDrillOutcome.REFUSED_SOURCE_REACHABLE
    drill_commands = [argv for argv in runner.commands if argv[0] == "nc"]
    assert len(drill_commands) == 1
    assert_no_mutation(runner.commands, overlay.source.host, _ALLOWED)


def test_named_break_restart_source_turns_the_reading_red(tmp_path: Path) -> None:
    runner = _runner()
    overlay, _, _ = _job_and_drill(tmp_path, runner)
    host = overlay.source.host
    for planted in (
        ("ssh", host, "docker", "restart", "redpanda"),
        ("ssh", host, "systemctl", "stop", "redpanda"),
        ("ssh", host, "ls"),
        ("nc", "-l", host),
    ):
        with pytest.raises(AssertionError):
            assert_no_mutation([*runner.commands, planted], host, _ALLOWED)
    allowed_with_ssh = _ALLOWED | {"ssh"}
    with pytest.raises(AssertionError):
        assert_no_mutation(
            [("ssh", host, "docker", "restart", "x")], host, allowed_with_ssh
        )
    with pytest.raises(AssertionError):
        assert_no_mutation([("ssh", host, "ls")], host, allowed_with_ssh)


def test_the_export_text_is_what_the_snapshot_holds(tmp_path: Path) -> None:
    runner = _runner()
    overlay, _, _ = _job_and_drill(tmp_path, runner)
    run_dir = next((tmp_path / "snapshots").iterdir())
    first = json.loads((run_dir / "topic.jsonl").read_text().splitlines()[0])
    assert first == {"seq": 1, "row_id": "id-1", "text": "row 1 | text"}
    assert overlay.store.host != overlay.source.host
