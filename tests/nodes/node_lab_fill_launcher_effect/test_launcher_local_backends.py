# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill launcher on real files: the entry the tick calls, run against a synthetic host (OMN-24109).

Golden chain: a fire that placed lanes is completed and its result published. Error chain: a fire whose cut completion
notification names a written result file is completed, and the same fire with no file is degraded (the OMN-22431 control).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from omnimarket.nodes.node_lab_fill_launcher_effect.runtime_lab_fill_launcher import (
    main,
)

from .conftest import FIRE_ID, Host

pytestmark = pytest.mark.unit

TICK_KEYS = {
    "phase",
    "exit",
    "degraded_class",
    "result_channel",
    "result_truncated",
    "notice_line",
    "alert_exit",
    "alert_category",
}


def _run(
    host: Host, phase: str = "completed", exit_code: int = 0, alert_rc: int = 0
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    args = [
        "--fire-id",
        FIRE_ID,
        "--phase",
        phase,
        "--exit",
        str(exit_code),
        "--run-log",
        str(host.run_log),
        "--workflow-result-file",
        str(host.state / "results" / f"{FIRE_ID}.workflow-result.txt"),
        "--result-channel",
    ]
    with redirect_stdout(out):
        rc = main(args, host.env(alert_rc))
    return rc, json.loads(out.getvalue()) if out.getvalue() else {}


def test_golden_chain_a_fire_that_placed_lanes_is_completed_and_published(
    host: Host,
) -> None:
    host.write_result()
    rc, line = _run(host)
    assert rc == 0
    assert set(line) == TICK_KEYS
    assert (
        line["phase"],
        line["exit"],
        line["result_channel"],
        line["alert_exit"],
    ) == ("completed", 0, "read", None)
    published = (host.state / "lab-fill-latest.json").read_text(encoding="utf-8")
    assert json.loads(published)["fire_id"] == FIRE_ID
    assert published == json.dumps(json.loads(published), indent=2, sort_keys=True)
    assert not host.alert_sent.exists()


def test_golden_chain_an_idle_fire_alerts_once_and_names_the_missing_snapshot(
    host: Host,
) -> None:
    host.write_result(
        free_slots=3,
        dispatched=0,
        idle_alarm=True,
        friction_recorded=True,
        alert_text="LAB IDLE run=r details",
    )
    rc, line = _run(host)
    assert rc == 0
    assert (line["phase"], line["alert_category"], line["alert_exit"]) == (
        "completed",
        "lab_fill_idle",
        0,
    )
    sent = host.alert_sent.read_text(encoding="utf-8").splitlines()
    assert len(sent) == 1
    assert sent[0].startswith(
        "lab_fill_idle floor row stoppage:lab-idle | cause=linear-snapshot-absent:linear-"
    )


def test_golden_chain_the_snapshot_of_the_day_is_read_under_the_registry_root(
    host: Host,
) -> None:
    import datetime

    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    snapshots = host.home / ".claude_scratch" / "plans-today"
    snapshots.mkdir(parents=True)
    (snapshots / f"linear-{day}.json").write_text("{}", encoding="utf-8")
    host.write_result(
        free_slots=3,
        dispatched=0,
        idle_alarm=True,
        friction_recorded=True,
        alert_text="LAB IDLE run=r details",
    )
    _run(host)
    assert "linear-snapshot-absent" not in host.alert_sent.read_text(encoding="utf-8")


def test_error_chain_cut_notification_with_a_written_result_file_is_completed(
    host: Host,
) -> None:
    host.write_result()
    host.run_log.write_text(
        "WORKFLOW_RESULT_TRUNCATED the result ends mid-structure\n", encoding="utf-8"
    )
    rc, line = _run(host)
    assert rc == 0
    assert (
        line["phase"],
        line["exit"],
        line["result_channel"],
        line["result_truncated"],
        line["degraded_class"],
    ) == (
        "completed",
        0,
        "read",
        True,
        None,
    )


def test_error_chain_cut_notification_with_no_result_file_stays_degraded(
    host: Host,
) -> None:
    """FAILING CONTROL for OMN-22431: the same cut fire with no written result file is still degraded."""
    host.run_log.write_text(
        "WORKFLOW_RESULT_TRUNCATED the result ends mid-structure\n", encoding="utf-8"
    )
    rc, line = _run(host)
    assert rc == 0
    assert (
        line["phase"],
        line["exit"],
        line["degraded_class"],
        line["result_truncated"],
    ) == (
        "degraded",
        70,
        "workflow-result-truncated",
        True,
    )


def test_error_chain_a_refused_fire_alerts_and_an_undelivered_alert_is_in_the_verdict(
    host: Host,
) -> None:
    host.run_log.write_text("You've hit your weekly limit\n", encoding="utf-8")
    rc, line = _run(host, phase="failed", exit_code=1, alert_rc=2)
    assert rc == 0
    assert (
        line["phase"],
        line["degraded_class"],
        line["alert_category"],
        line["alert_exit"],
    ) == (
        "failed",
        "lab-fill-claude-refused",
        "lab_fill_refused",
        2,
    )


def test_error_chain_missing_alert_library_reports_the_overlay_unavailable_exit(
    host: Host,
) -> None:
    host.write_result(
        free_slots=3,
        dispatched=0,
        idle_alarm=True,
        friction_recorded=True,
        alert_text="LAB IDLE run=r details",
    )
    env = host.env()
    env["ONEX_ALERT_CHANNEL_LIB"] = str(host.home / "absent.sh")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(
            [
                "--fire-id",
                FIRE_ID,
                "--phase",
                "completed",
                "--exit",
                "0",
                "--run-log",
                str(host.run_log),
                "--result-channel",
            ],
            env,
        )
    line = json.loads(out.getvalue())
    assert rc == 0
    assert (line["phase"], line["degraded_class"], line["alert_exit"]) == (
        "degraded",
        "lab-idle-alert-undelivered",
        3,
    )


def test_error_chain_overlay_missing_refuses_with_78(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    env = host.env()
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(host.home / "no-such-root")
    rc = main(
        [
            "--fire-id",
            FIRE_ID,
            "--phase",
            "completed",
            "--exit",
            "0",
            "--run-log",
            str(host.run_log),
        ],
        env,
    )
    captured = capsys.readouterr()
    assert rc == 78
    assert captured.out == ""
    assert "lab-fill-launcher: REFUSED" in captured.err


def test_error_chain_missing_state_directory_refuses_with_78(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    env = host.env()
    env["OMNI_HOME"] = str(host.home / "elsewhere")
    rc = main(
        [
            "--fire-id",
            FIRE_ID,
            "--phase",
            "completed",
            "--exit",
            "0",
            "--run-log",
            str(host.run_log),
        ],
        env,
    )
    assert rc == 78
    assert "state directory is missing" in capsys.readouterr().err
