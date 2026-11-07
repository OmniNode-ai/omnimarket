# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect reading writer and schedule: golden chain, error chain, named
failing controls, and the schedule's install, refusal and rollback (OMN-22860)."""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_reading_writer import (
    HandlerExposureReadingWriter,
    load_reading_topics,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.schedule import (
    ExposureScheduler,
    load_schedule,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_reading import (
    ModelExposureReadingCommand,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelExposureReadingOverlay,
    load_reading_overlay,
)
from omnimarket.nodes.node_runner_group_exposure_effect.runtime_exposure_probe import (
    main,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runner_group_exposure_effect"
)
REPO = "example-org/example-repo"
PATH = ".github/workflows/build.yml"


def _overlay_fields(
    *, triggers: Sequence[str] = ("push",), read_failure: str | None = None
) -> dict[str, Any]:
    fields = yaml.safe_load((NODE_DIR / "overlay.reading.example.yaml").read_text())
    fixture = fields["source"]["fixture"]
    fixture["files"][0]["triggers"] = list(triggers)
    if read_failure:
        fixture["read_failure"] = read_failure
        fixture["group"] = None
        fixture["files"] = []
    return copy.deepcopy(fields)


def _overlay(**kwargs: Any) -> ModelExposureReadingOverlay:
    return ModelExposureReadingOverlay.model_validate(_overlay_fields(**kwargs))


class _Appender:
    def __init__(self, status: str = "accepted") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append((rows, request_id))
        return ModelAppendOutcome.model_validate({"status": self.status})


def _writer(
    overlay: ModelExposureReadingOverlay, appender: Any
) -> HandlerExposureReadingWriter:
    return HandlerExposureReadingWriter(
        overlay=overlay,
        host="example-host",
        probe=HandlerExposureProbe(overlay=overlay),
        appender=appender,
    )


def _command(write: bool = True) -> ModelExposureReadingCommand:
    return ModelExposureReadingCommand(correlation_id="reading-1", now=NOW, write=write)


def _cells(row: str) -> dict[str, str]:
    parts = [p.strip() for p in row.strip().split(" | ")]
    return dict(p.split("=", 1) for p in parts[2:] if "=" in p)


# -- golden chain --------------------------------------------------------------------------


def test_golden_chain_a_clean_group_writes_one_working_row() -> None:
    ledger = InMemoryLedger()
    outcome = _writer(_overlay(), ledger).handle(_command())
    assert outcome.verdict is EnumReadingVerdict.WORKING
    assert outcome.outcome is EnumReadingOutcome.READ
    assert outcome.ledger == "written"
    assert not outcome.failed
    assert outcome.row is not None
    cells = _cells(outcome.row)
    assert cells["verdict"] == "WORKING"
    assert cells["src"] == "runner-group-exposure"
    assert cells["lane"] == "runner-group-exposure"
    assert cells["g7_public_reach_files"] == "WORKING"
    assert outcome.row.split(" | ")[1] == "STATUS"
    assert cells["key"] == outcome.row_key


def test_golden_chain_the_same_run_again_is_already_written() -> None:
    appender = _Appender("duplicate")
    outcome = _writer(_overlay(), appender).handle(_command())
    assert outcome.ledger == "already-written"
    assert not outcome.failed
    again = _writer(_overlay(), appender).handle(_command())
    assert again.row_key == outcome.row_key
    assert appender.calls[0][1] == appender.calls[1][1]


def test_golden_chain_the_terminal_event_is_the_written_topic() -> None:
    outcome = _writer(_overlay(), InMemoryLedger()).handle(_command())
    topic, body = HandlerExposureReadingWriter.event_for(outcome)
    assert topic == load_reading_topics()["reading_written"]
    assert body["verdict"] == "WORKING"
    assert "row" not in body


def test_a_probe_run_appends_nothing() -> None:
    appender = _Appender()
    outcome = _writer(_overlay(), appender).handle(_command(write=False))
    assert outcome.ledger == "not-written"
    assert appender.calls == []


# -- named failing control -----------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger", ["pull_request_target", "pull_request", "issue_comment"]
)
def test_injected_break_a_public_trigger_writes_a_not_working_row(trigger: str) -> None:
    broken = _writer(_overlay(triggers=("push", trigger)), InMemoryLedger()).handle(
        _command()
    )
    clean = _writer(_overlay(), InMemoryLedger()).handle(_command())
    assert clean.verdict is EnumReadingVerdict.WORKING
    assert broken.verdict is EnumReadingVerdict.NOT_WORKING
    assert broken.outcome is EnumReadingOutcome.READ
    assert not broken.failed, "a NOT_WORKING reading that was read is not a failed run"
    assert broken.row is not None
    cells = _cells(broken.row)
    assert cells["verdict"] == "NOT_WORKING"
    assert cells["g7_public_reach_files"] == "NOT_WORKING"
    assert cells["reason"].startswith("g7-public-reach-files")
    assert broken.row_key != clean.row_key or broken.row != clean.row


# -- error chain ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure", ["org-read-denied", "unreadable", "budget-exhausted"]
)
def test_error_chain_an_unread_group_writes_an_unknown_row_and_fails_the_run(
    failure: str,
) -> None:
    outcome = _writer(_overlay(read_failure=failure), InMemoryLedger()).handle(
        _command()
    )
    assert outcome.verdict is EnumReadingVerdict.UNKNOWN
    assert outcome.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcome.failed
    assert outcome.ledger == "written", "the attempt is still recorded"
    assert outcome.row is not None
    assert _cells(outcome.row)["reason"] == failure
    topic, _ = HandlerExposureReadingWriter.event_for(outcome)
    assert topic == load_reading_topics()["reading_failed"]


@pytest.mark.parametrize("status", ["refused", "pending", "error"])
def test_error_chain_an_append_that_does_not_land_fails_the_run(status: str) -> None:
    outcome = _writer(_overlay(), _Appender(status)).handle(_command())
    assert outcome.ledger == f"append-failed:{status}"
    assert outcome.failed
    topic, _ = HandlerExposureReadingWriter.event_for(outcome)
    assert topic == load_reading_topics()["reading_failed"]


def test_error_chain_a_writing_run_with_no_appender_fails() -> None:
    outcome = _writer(_overlay(), None).handle(_command())
    assert outcome.ledger == "append-failed:no-appender"
    assert outcome.failed


# -- overlay and contract ------------------------------------------------------------------


def test_the_shipped_reading_example_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, _overlay_fields())
    overlay = load_reading_overlay()
    assert overlay.host("example-host") is not None
    assert overlay.host("absent") is None
    assert len(overlay.overlay_hash()) == 24


@pytest.mark.parametrize("key", ["ledger_append", "publish", "hosts"])
def test_a_reading_overlay_without_a_key_refuses(key: str) -> None:
    fields = _overlay_fields()
    del fields[key]
    with pytest.raises(ValueError, match=key):
        ModelExposureReadingOverlay.model_validate(fields)


def test_a_host_declared_twice_refuses() -> None:
    fields = _overlay_fields()
    fields["hosts"] = [fields["hosts"][0], fields["hosts"][0]]
    with pytest.raises(ValueError, match="declared twice"):
        ModelExposureReadingOverlay.model_validate(fields)


def test_the_contract_declares_the_reading_topics_and_the_schedule() -> None:
    topics = load_reading_topics()
    assert topics["reading_written"].endswith(
        "runner-group-exposure-reading-written.v1"
    )
    assert topics["reading_failed"].endswith("runner-group-exposure-reading-failed.v1")
    schedule = load_schedule()
    assert schedule.unit_name == "onex-runner-group-exposure-hourly"
    assert schedule.cadence.on_calendar == "*-*-* *:35:00"


# -- the schedule: install, refusal, rollback ----------------------------------------------


class _Recorder:
    def __init__(self, venv: Path) -> None:
        self.commands: list[list[str]] = []
        self.venv = venv

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _scheduler(tmp_path: Path, venv: str) -> tuple[ExposureScheduler, _Recorder, Path]:
    fields = _overlay_fields()
    fields["hosts"][0]["venv"] = venv
    overlay = ModelExposureReadingOverlay.model_validate(fields)
    home = tmp_path / "home"
    home.mkdir()
    recorder = _Recorder(tmp_path)
    return (
        ExposureScheduler(
            schedule=load_schedule(), overlay=overlay, home=home, run=recorder
        ),
        recorder,
        home,
    )


def _make_venv(root: Path, venv: str) -> None:
    python = root / venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")


def test_install_writes_the_units_and_enables_the_timer(tmp_path: Path) -> None:
    venv = ".onex_state/installed-units/example-unit/omnimarket/.venv"
    root = tmp_path / "registry"
    _make_venv(root, venv)
    scheduler, recorder, home = _scheduler(tmp_path, venv)
    result = scheduler.install(
        "example-host", omni_root=root, overlay_root=root / "overlays"
    )
    assert result.exit_code == 0, result.stderr
    service = (
        home / ".config/systemd/user/onex-runner-group-exposure-hourly.service"
    ).read_text()
    timer = (
        home / ".config/systemd/user/onex-runner-group-exposure-hourly.timer"
    ).read_text()
    assert (
        f"ExecStart={root}/{venv}/bin/python -m omnimarket.nodes.{NODE_NAME} hourly"
        in service
    )
    assert (
        f"EnvironmentFile=-{home}/.config/onex/example-group-read-token.env" in service
    )
    assert "OnCalendar=*-*-* *:35:00" in timer
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-runner-group-exposure-hourly.timer",
    ] in recorder.commands


def test_install_refuses_a_unit_that_runs_from_a_worktree(tmp_path: Path) -> None:
    venv = "omni_worktrees/T-1/omnimarket/.venv"
    root = tmp_path / "registry"
    _make_venv(root, venv)
    scheduler, recorder, home = _scheduler(tmp_path, venv)
    result = scheduler.install(
        "example-host", omni_root=root, overlay_root=root / "overlays"
    )
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr
    assert not (home / ".config/systemd/user").exists()
    assert recorder.commands == []


def test_install_refuses_a_venv_with_no_interpreter(tmp_path: Path) -> None:
    scheduler, recorder, _ = _scheduler(
        tmp_path, ".onex_state/installed-units/none/.venv"
    )
    result = scheduler.install(
        "example-host", omni_root=tmp_path / "registry", overlay_root=tmp_path
    )
    assert result.exit_code == 1
    assert "no venv interpreter" in result.stderr
    assert recorder.commands == []


def test_uninstall_is_the_rollback_and_removes_both_units(tmp_path: Path) -> None:
    venv = ".onex_state/installed-units/example-unit/omnimarket/.venv"
    root = tmp_path / "registry"
    _make_venv(root, venv)
    scheduler, recorder, home = _scheduler(tmp_path, venv)
    scheduler.install("example-host", omni_root=root, overlay_root=root / "overlays")
    result = scheduler.uninstall("example-host")
    assert result.exit_code == 0
    assert list((home / ".config/systemd/user").iterdir()) == []
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-runner-group-exposure-hourly.timer",
    ] in recorder.commands


def test_an_undeclared_host_is_refused(tmp_path: Path) -> None:
    scheduler, _, _ = _scheduler(tmp_path, ".onex_state/x/.venv")
    for verb in (scheduler.uninstall, scheduler.run_now, scheduler.status):
        assert verb("absent").exit_code == 1


# -- the CLI --------------------------------------------------------------------------------


def test_the_probe_verb_reads_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, _overlay_fields())
    monkeypatch.setenv("EXAMPLE_HOST_NAME", "example-host")
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "verdict=WORKING" in out
    assert "ledger=not-written" in out


def test_the_probe_verb_exits_one_when_the_group_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, _overlay_fields(read_failure="unreadable")
    )
    monkeypatch.setenv("EXAMPLE_HOST_NAME", "example-host")
    assert main(["probe"]) == 1
    assert "verdict=UNKNOWN" in capsys.readouterr().out


def test_a_missing_host_name_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, _overlay_fields())
    monkeypatch.delenv("EXAMPLE_HOST_NAME", raising=False)
    assert main(["hourly"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
