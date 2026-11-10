# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red hourly seam reading is read again soon, so a redeploy window does not hold piece 3 red (OMN-23201).

The incident: the dev runtime was redeployed a minute before the :35 hourly run, the seam gave no
result by its deadline, and the NOT_WORKING row stayed the newest row for the rest of the hour after
the seam answered again. Golden: the red run is followed by a re-read that reads WORKING and lands
the newest row. Error: a re-read that finds the seam still silent writes a red row and counts, the
re-reads stop at the contract's attempts, a working last run starts no re-read, and a drill neither
records state nor starts one. The red rows are never removed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_seam_deadline_probe_effect import runtime_seam_deadline
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers import recheck
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.recheck import (
    EnumRecheckDecision,
    decide_recheck,
    record_run,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.schedule import (
    SeamDeadlineScheduler,
    load_schedule,
    render_recheck_systemd,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_deadline_probe_effect/overlay.example.yaml"
)


def _overlay() -> ModelSeamDeadlineOverlay:
    return ModelSeamDeadlineOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )


class _Clock:
    """Wall time that moves five minutes at each read, as the recheck ticks do."""

    def __init__(self) -> None:
        self.at = datetime(2026, 11, 9, 14, 35, tzinfo=UTC)

    def now(self, tz: object = None) -> datetime:
        self.at += timedelta(minutes=5)
        return self.at


class _Runtime:
    """The seam's node as the CLI sees it: silent while booting, answering once it is up."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.overlay = _overlay()
        self.ledger = InMemoryLedger()
        self.booting = True
        seam = self.overlay.seams[0]

        def node(payload: dict[str, str | int | bool]) -> dict[str, object] | None:
            if self.booting or payload["query"] != seam.request["query"]:
                return None
            return {"query": payload["query"], "rows": []}

        self.bus = InMemorySeamBus({seam.command_topic: node})
        monkeypatch.setattr(runtime_seam_deadline, "load_overlay", lambda: self.overlay)
        monkeypatch.setitem(
            runtime_seam_deadline._SEAM_BUSES, "inmemory", lambda _lane: self.bus
        )
        monkeypatch.setattr(
            runtime_seam_deadline, "build_appender", lambda _route, _lane: self.ledger
        )
        clock = _Clock()
        monkeypatch.setattr(
            runtime_seam_deadline,
            "datetime",
            type("FakeDatetime", (), {"now": staticmethod(clock.now)}),
        )
        monkeypatch.setenv("EXAMPLE_HOST_NAME", "test-host")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    def run(self, *arguments: str) -> int:
        return runtime_seam_deadline.main(list(arguments))

    def verdicts(self) -> list[str]:
        return [
            "WORKING" if "verdict=WORKING" in line else "NOT_WORKING"
            for line in self.ledger.lines
        ]


def test_golden_chain_the_seam_answers_again_and_the_recheck_reads_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The incident replayed: red at the hourly run, WORKING at the first recheck after the boot."""
    runtime = _Runtime(monkeypatch, tmp_path)

    assert runtime.run("read") == 0  # the hourly run lands in the boot window
    assert runtime.verdicts() == ["NOT_WORKING"]

    runtime.booting = False  # the runtime has attached its contracts
    assert runtime.run("read", "--recheck") == 0

    assert runtime.verdicts() == ["NOT_WORKING", "WORKING"]  # the red row stays
    assert "drill=no" in runtime.ledger.lines[-1]
    assert (
        decide_recheck(
            tmp_path / load_schedule().state_directory, load_schedule().recheck
        )
        is EnumRecheckDecision.SKIP_LAST_RUN_WORKING
    )


def test_error_chain_a_recheck_while_the_seam_is_still_silent_writes_red_and_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _Runtime(monkeypatch, tmp_path)
    attempts = load_schedule().recheck.attempts

    runtime.run("read")
    for _ in range(attempts):
        assert runtime.run("read", "--recheck") == 0
    assert runtime.verdicts() == ["NOT_WORKING"] * (1 + attempts)

    # The re-reads are used up: the hourly cadence resumes, no further row is written.
    assert runtime.run("read", "--recheck") == 0
    assert len(runtime.ledger.lines) == 1 + attempts

    # The next hourly run restarts the count.
    runtime.booting = False
    runtime.run("read")
    assert runtime.verdicts()[-1] == "WORKING"


def test_error_chain_a_recheck_after_a_working_run_or_with_no_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = _Runtime(monkeypatch, tmp_path)

    assert runtime.run("read", "--recheck") == 0
    assert "RECHECK skipped reason=skip-no-run-recorded" in capsys.readouterr().out

    runtime.booting = False
    runtime.run("read")
    assert runtime.run("read", "--recheck") == 0
    assert "RECHECK skipped reason=skip-last-run-working" in capsys.readouterr().out
    assert runtime.verdicts() == ["WORKING"]
    assert len(runtime.bus.sent) == 1  # a skipped recheck demands nothing from the seam


def test_error_chain_a_drill_neither_records_state_nor_starts_a_recheck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _Runtime(monkeypatch, tmp_path)
    runtime.booting = False

    runtime.run("read", "--fault", "unanswerable-command")  # drill=yes, NOT_WORKING
    assert "drill=yes" in runtime.ledger.lines[0]
    assert not (tmp_path / load_schedule().state_directory).exists()
    assert runtime.run("read", "--recheck") == 0
    assert len(runtime.ledger.lines) == 1


def test_a_corrupt_state_file_skips_rather_than_inventing_a_verdict(
    tmp_path: Path,
) -> None:
    (tmp_path / recheck.STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    assert (
        decide_recheck(tmp_path, load_schedule().recheck)
        is EnumRecheckDecision.SKIP_STATE_UNREADABLE
    )
    record_run(tmp_path, working=False, recheck=False)  # the next real run repairs it
    assert decide_recheck(tmp_path, load_schedule().recheck) is EnumRecheckDecision.RUN


def test_the_recheck_flag_belongs_to_the_read_verb_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Runtime(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        runtime_seam_deadline.main(["probe", "--recheck"])


def _scheduler(tmp_path: Path, calls: list[list[str]]) -> SeamDeadlineScheduler:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")

    return SeamDeadlineScheduler(
        schedule=load_schedule(),
        overlay=_overlay(),
        home=tmp_path / "home",
        run=run,
    )


def test_install_writes_the_recheck_timer_and_uninstall_removes_it(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    scheduler = _scheduler(tmp_path, calls)
    result = scheduler.install(
        "example-lab-host", omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    service = (unit_dir / "onex-seam-deadline-recheck.service").read_text()
    timer = (unit_dir / "onex-seam-deadline-recheck.timer").read_text()
    assert (
        "-m omnimarket.nodes.node_seam_deadline_probe_effect read --recheck" in service
    )
    assert 'Environment="EXAMPLE_HOST_NAME=example-lab-host"' in service
    assert "OnActiveSec=5min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "Unit=onex-seam-deadline-recheck.service" in timer
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-seam-deadline-recheck.timer",
    ] in calls
    # The hourly unit is untouched: same cadence, same verb.
    hourly = (unit_dir / "onex-seam-deadline-hourly.timer").read_text()
    assert "OnCalendar=*-*-* *:35:00" in hourly

    scheduler.uninstall("example-lab-host")
    assert not list(unit_dir.iterdir())


def test_the_recheck_unit_does_not_change_the_deadline_or_the_hourly_bound() -> None:
    schedule = load_schedule()
    assert schedule.timeout_seconds == 300
    assert schedule.cadence.on_calendar == "*-*-* *:35:00"
    assert _overlay().seams[0].deadline_seconds == 30
    assert _overlay().publish.publish_max_age_seconds == 7200
    service, _ = render_recheck_systemd(
        schedule,
        _overlay().schedule_hosts[0],
        omni_root=Path("/root"),
        home=Path("/home/u"),
        overlay_root=Path("/ovl"),
    )
    assert "TimeoutStartSec=300" in service
