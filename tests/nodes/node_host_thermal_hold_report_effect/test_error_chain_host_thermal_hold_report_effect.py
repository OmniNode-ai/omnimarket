# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every way a run can fail ends in a typed error and loses nothing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_thermal_hold_report_effect import (
    runtime_host_thermal_hold_tick as tick,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.models import (
    EnumGuardReadState,
    ModelGuardRead,
)

from .conftest import LAST, LATCH, outbox_row, present

pytestmark = pytest.mark.unit

OPEN = {"open": True, "trip_key": "2026-11-15T01:47:02Z", "hottest_c": 90.6}


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (EnumGuardReadState.UNREADABLE, "latch_unreadable"),
        (EnumGuardReadState.SHAPE_CHANGED, "shape_changed"),
    ],
)
def test_a_latch_that_cannot_be_used_never_reads_as_a_release(
    handler, guard, episodes, appender, request_for, state, code
) -> None:
    episodes.record = dict(OPEN)
    guard.latch = ModelGuardRead(state, None, "cap.json: detail")
    result = handler.handle(request_for())
    assert appender.sent == []
    assert result.alarm == "none"
    assert result.errors[0][0] == code
    assert not result.ok
    assert episodes.record == OPEN


def test_an_unreadable_outbox_is_an_error_and_is_not_claimed(
    handler, guard, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    guard.outbox = None
    result = handler.handle(request_for())
    assert ("outbox_unreadable", "the outbox cannot be read") in result.errors
    assert result.alarm == "trip"
    assert guard.claimed is None


def test_an_unreadable_episode_decides_no_alarm_and_still_delivers(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    guard.outbox = [outbox_row(1)]
    episodes.record = None
    result = handler.handle(request_for())
    assert result.alarm == "none"
    assert result.delivered == 1
    assert result.errors[0][0] == "episode_unreadable"


def test_a_failed_settle_leaves_the_claim_and_the_next_run_delivers_from_it(
    handler, guard, appender, request_for
) -> None:
    guard.outbox = [outbox_row(1), outbox_row(2)]
    guard.settle_error = OSError("disk full")
    first = handler.handle(request_for())
    assert first.errors[0][0] == "settle_failed"
    assert not first.ok
    assert guard.claimed == [outbox_row(1), outbox_row(2)]  # nothing lost
    guard.settle_error = None
    appender.default = "duplicate"  # the ledger knows them: the same request ids
    second = handler.handle(request_for())
    assert second.delivered == 2
    assert second.ok
    assert guard.claimed is None
    assert guard.outbox == []


def test_an_unwritable_episode_is_an_error(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    episodes.save_error = OSError("read-only")
    result = handler.handle(request_for())
    assert result.errors[0][0] == "episode_unwritable"
    assert not result.ok


def test_an_unreadable_last_reading_is_an_error_and_the_alarm_still_goes(
    handler, guard, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    guard.last = ModelGuardRead(EnumGuardReadState.ABSENT)
    result = handler.handle(request_for())
    assert result.errors[0][0] == "last_unreadable"
    assert result.alarm_status == "accepted"
    assert not result.ok


def test_an_alarm_the_ledger_refuses_is_reported_and_retried_under_its_id(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    appender.default = "refused"
    first = handler.handle(request_for())
    assert first.alarm_status == "refused"
    assert not first.ok
    second = handler.handle(request_for())
    assert second.alarm_request_id == first.alarm_request_id
    assert len(appender.sent) == 2


def test_the_overlay_without_a_key_is_refused_naming_it(overlay) -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_host_thermal_hold_report_effect.overlay import (
        ModelHostThermalHoldReportOverlay,
    )

    document = overlay.model_dump()
    del document["to_lane"]
    with pytest.raises(ValidationError, match="to_lane"):
        ModelHostThermalHoldReportOverlay.model_validate(document)
    with pytest.raises(ValueError, match="not a host of the"):
        overlay.host_row("nowhere")


def _tick_env(monkeypatch, tmp_path: Path, overlay, *, command: str | None) -> Path:
    root = tmp_path / "overlays" / "node_host_thermal_hold_report_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(json.loads(overlay.model_dump_json()))
    )
    home = tmp_path / "home"
    state = home / overlay.host_row("lab-a").guard_state_dir
    state.mkdir(parents=True)
    (state / overlay.latch_file).write_text(json.dumps(LATCH))
    (state / overlay.last_file).write_text(json.dumps(LAST))
    (state / overlay.outbox_file).write_text(outbox_row(1) + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("ONEX_HOST_NAME", "lab-a")
    if command is None:
        monkeypatch.delenv("ONEX_LEDGER_BUS_APPEND_COMMAND", raising=False)
    else:
        monkeypatch.setenv("ONEX_LEDGER_BUS_APPEND_COMMAND", command)
    return state


def _append_script(tmp_path: Path, status: str, exit_code: int) -> str:
    script = tmp_path / "append.py"
    script.write_text(
        "import sys\nsys.stdin.read()\n"
        f'print(\'{{"status": "{status}"}}\')\nsys.exit({exit_code})\n'
    )
    return f"{sys.executable} {script}"


def test_the_tick_probe_answers_without_an_overlay_or_a_write(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    assert tick.main(["--probe"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "probe_ok"}
    assert list(tmp_path.iterdir()) == []


def test_the_tick_refuses_a_missing_overlay_and_a_missing_append_command(
    tmp_path, monkeypatch, capsys, overlay
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty))
    assert tick.main(["--state-dir", str(tmp_path / "unit")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    _tick_env(monkeypatch, tmp_path, overlay, command=None)
    assert tick.main(["--state-dir", str(tmp_path / "unit")]) == 2
    assert (
        "ONEX_LEDGER_BUS_APPEND_COMMAND"
        in json.loads(capsys.readouterr().out)["detail"]
    )


def test_the_tick_delivers_and_writes_its_heartbeat(
    tmp_path, monkeypatch, capsys, overlay
) -> None:
    _tick_env(
        monkeypatch, tmp_path, overlay, command=_append_script(tmp_path, "accepted", 0)
    )
    unit = tmp_path / "unit"
    assert tick.main(["--state-dir", str(unit)]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["status"] == "ok"
    assert line["alarm"] == "trip"
    assert line["delivered"] == 1
    assert json.loads((unit / "last-run.json").read_text())["exit_code"] == 0


def test_the_tick_exits_3_and_keeps_the_rows_when_the_ledger_refuses(
    tmp_path, monkeypatch, capsys, overlay
) -> None:
    state = _tick_env(
        monkeypatch, tmp_path, overlay, command=_append_script(tmp_path, "refused", 65)
    )
    assert tick.main(["--state-dir", str(tmp_path / "unit")]) == 3
    line = json.loads(capsys.readouterr().out)
    assert line["status"] == "undone"
    assert len(line["refused"]) == 1
    assert (state / overlay.refused_file).read_text().startswith(outbox_row(1))
