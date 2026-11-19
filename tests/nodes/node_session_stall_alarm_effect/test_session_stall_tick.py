# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the unit's module, from overlays found under the overlay roots (OMN-25405)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_session_stall_alarm_effect import (
    runtime_session_stall_tick as tick,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _roots(tmp_path: Path, transcript_glob: str) -> Path:
    roots = tmp_path / "overlays"
    stall = yaml.safe_load(
        (NODES / "node_session_stall_alarm_effect" / "overlay.example.yaml").read_text()
    )
    stall["transcript_glob"] = transcript_glob
    for node, document in (
        ("node_session_stall_alarm_effect", stall),
        (
            "node_lab_alarm_deliver_effect",
            yaml.safe_load(
                (
                    NODES / "node_lab_alarm_deliver_effect" / "overlay.example.yaml"
                ).read_text()
            ),
        ),
    ):
        (roots / node).mkdir(parents=True)
        (roots / node / "overlay.yaml").write_text(yaml.safe_dump(document))
    return roots


def _transcript(path: Path, enqueued_minutes_ago: int) -> None:
    at = datetime.now(UTC) - timedelta(minutes=enqueued_minutes_ago)
    path.write_text(
        json.dumps(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "content": "a notice",
            }
        )
        + "\n"
    )


def test_a_tick_reads_the_transcript_and_prints_one_line_for_a_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _transcript(tmp_path / "session.jsonl", 20)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_roots(tmp_path, str(tmp_path / "*.jsonl")))
    )
    state = tmp_path / "state"
    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("session-stall tick ")
    assert "outcome=stalled" in out[0]
    assert "delivery=delivered" in out[0]
    assert "reasons=oldest-enqueue-undequeued" in out[0]
    assert "a notice" not in out[0]
    evidence = json.loads((state / "last-run.json").read_text())
    assert evidence["exit_code"] == 0
    assert evidence["line"] == out[0]


def test_a_tick_on_a_healthy_session_prints_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _transcript(tmp_path / "session.jsonl", 2)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_roots(tmp_path, str(tmp_path / "*.jsonl")))
    )
    assert (
        tick.main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "s")])
        == 0
    )
    assert "outcome=ok" in capsys.readouterr().out


def test_a_tick_with_no_transcript_exits_3_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_roots(tmp_path, str(tmp_path / "none-*.jsonl"))),
    )
    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "s")])
    assert code == tick.EXIT_UNREADABLE
    assert "outcome=indeterminate" in capsys.readouterr().out


def test_a_probe_prints_the_reading_and_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _transcript(tmp_path / "session.jsonl", 20)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_roots(tmp_path, str(tmp_path / "*.jsonl")))
    )
    state = tmp_path / "state"
    tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state), "--probe"])
    out = capsys.readouterr().out
    assert "outcome=stalled" in out
    assert "delivery=skipped" in out
    assert not state.exists()


def test_the_unit_probe_help_request_runs_no_tick(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        tick.main(["--help"])
    assert stopped.value.code == 0
    assert "usage" in capsys.readouterr().out
