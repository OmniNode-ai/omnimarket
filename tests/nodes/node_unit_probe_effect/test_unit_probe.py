# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-unit probe through direct calls: launchd and systemd reads, the pool answer, the pool tick wiring."""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_unit_probe_effect import NodeUnitProbeEffect
from omnimarket.nodes.node_unit_probe_effect.handlers import handler_unit_probe as probe
from omnimarket.nodes.node_unit_probe_effect.models import (
    ModelUnitProbeRequest,
    ModelUnitProbeResult,
)

pytestmark = pytest.mark.unit

TRIGGER = "Sat 2026-11-14 08:00:00 UTC"
TRIGGERED = datetime(2026, 11, 14, 8, 0, 0, tzinfo=UTC)


def request(
    manager: str = "launchd", out_paths: tuple[str, ...] = ()
) -> ModelUnitProbeRequest:
    return ModelUnitProbeRequest(
        manager=manager,
        label="ai.example.tick",
        name="onex-example.timer",
        out_paths=out_paths,
        landing_placement_path="/nonexistent/landing_placement.py",
    )


def completed(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def pool_answer(unit: str, active: str, trigger: str = TRIGGER) -> str:
    return (
        "@@systemd\n@@scope=system\n"
        f"Id={unit}\nActiveState={active}\nLastTriggerUSec={trigger}\n\n"
        "@@scope=user\n"
    )


def test_model_refuses_unknown_field() -> None:
    with pytest.raises(ValueError, match="bogus"):
        ModelUnitProbeResult(loaded=True, last_run=None, bogus=1)  # type: ignore[call-arg]


def test_launchd_unit_loaded_with_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("ok")
    os.utime(heartbeat, (TRIGGERED.timestamp(), TRIGGERED.timestamp()))
    monkeypatch.setattr(probe.subprocess, "run", lambda *_a, **_k: completed())
    result = NodeUnitProbeEffect().handle(request(out_paths=(str(heartbeat),)))
    assert result == ModelUnitProbeResult(loaded=True, last_run=TRIGGERED, failure="")


def test_launchd_unit_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *_a, **_k: completed(returncode=113)
    )
    result = probe.HandlerUnitProbe().handle(request())
    assert result.loaded is False
    assert result.last_run is None


def test_launchd_failing_census_record_is_the_cause_not_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "heartbeat"
    (tmp_path / "census.json").write_text(
        json.dumps({"status": "FAIL", "error": "boom", "detail": "no kafka"})
    )
    monkeypatch.setattr(probe.subprocess, "run", lambda *_a, **_k: completed())
    result = probe.HandlerUnitProbe().handle(request(out_paths=(str(heartbeat),)))
    assert result.loaded is True
    assert result.last_run is None
    assert result.failure == "boom; no kafka"


def test_launchd_census_record_older_than_heartbeat_is_not_a_cause(
    tmp_path: Path,
) -> None:
    census = tmp_path / "census.json"
    census.write_text(json.dumps({"status": "FAIL"}))
    os.utime(census, (100.0, 100.0))
    assert probe.failure_cause(census) == "status FAIL, no cause recorded"
    assert probe.record_failure((str(tmp_path / "heartbeat"),), TRIGGERED) == ""
    assert (
        probe.record_failure((str(tmp_path / "heartbeat"),), None)
        == "status FAIL, no cause recorded"
    )


def test_failure_cause_ignores_pass_and_unreadable(tmp_path: Path) -> None:
    ok = tmp_path / "census.json"
    ok.write_text(json.dumps({"status": "PASS", "error": "x"}))
    assert probe.failure_cause(ok) == ""
    assert probe.failure_cause(tmp_path / "missing.json") == ""
    ok.write_text("not json")
    assert probe.failure_cause(ok) == ""


def test_declared_paths_skips_placeholders() -> None:
    paths = probe.declared_paths(("{{state}}/hb", "$HOME/hb", "/tmp/hb"))
    assert [str(p) for p in paths] == ["/tmp/hb", "/tmp/census.json"]


def test_systemd_unit_active_in_user_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        user = "--user" in argv
        if "ActiveState" in argv:
            return completed("active\n" if user else "inactive\n")
        return completed(TRIGGER + "\n")

    monkeypatch.setattr(probe.subprocess, "run", run)
    result = probe.HandlerUnitProbe().handle(request("systemd"))
    assert result == ModelUnitProbeResult(loaded=True, last_run=TRIGGERED, failure="")
    assert any("--user" in call for call in calls)


def test_systemd_unit_in_no_scope_is_unloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *_a, **_k: completed("inactive\n")
    )
    assert probe.HandlerUnitProbe().handle(request("systemd")).loaded is False


def test_host_without_systemctl_asks_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_systemctl(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(probe.subprocess, "run", no_systemctl)
    found = probe.PoolTimers(active={"onex-example.timer": TRIGGERED}, complete=True)
    monkeypatch.setattr(probe, "pool_timers", lambda _path: found)
    assert probe.HandlerUnitProbe().handle(request("systemd")) == ModelUnitProbeResult(
        loaded=True, last_run=TRIGGERED, failure=""
    )
    monkeypatch.setattr(
        probe, "pool_timers", lambda _path: probe.PoolTimers(complete=True)
    )
    assert probe.HandlerUnitProbe().handle(request("systemd")).loaded is False
    monkeypatch.setattr(
        probe, "pool_timers", lambda _path: probe.PoolTimers(complete=False)
    )
    unknown = probe.HandlerUnitProbe().handle(request("systemd"))
    assert unknown.loaded is None
    assert unknown.last_run is None


def test_launchd_without_launchctl_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_launchctl(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(probe.subprocess, "run", no_launchctl)
    assert probe.HandlerUnitProbe().handle(request()).loaded is None


def test_parse_pool_answer() -> None:
    assert probe.parse_pool_answer("@@nosystemd\n") == (False, {})
    assert probe.parse_pool_answer("garbage") is None
    assert (
        probe.parse_pool_answer(pool_answer("a.timer", "active") + "@@failed=user\n")
        is None
    )
    parsed = probe.parse_pool_answer(pool_answer("a.timer", "active"))
    assert parsed == (True, {"a.timer": (True, TRIGGERED)})


def test_pool_hosts_unreadable_placement_is_empty() -> None:
    assert probe.pool_hosts("/nonexistent/landing_placement.py") == {}
    assert probe.pool_targets("/nonexistent/landing_placement.py") == []


@dataclass
class Tick:
    name: str
    interval_s: float


@dataclass(frozen=True)
class Wiring:
    wired: bool = False
    unjudgeable: bool = False
    detail: str = "missing here"
    ran_at: str = ""


def tick_text(result: str, trigger: str = TRIGGER) -> str:
    return (
        "@@systemd\n@@scope=user\n"
        f"Id=onex-census.timer\nActiveState=active\nLastTriggerUSec={trigger}\n\n"
        f"Id=onex-census.service\nActiveState=inactive\nResult={result}\nExecMainStatus=0\n"
    )


def test_parse_tick_answer() -> None:
    run = probe.parse_tick_answer(tick_text("success"), "h1")
    assert run == probe.TickRun("h1", TRIGGERED, "success", "0")
    assert probe.parse_tick_answer("@@nosystemd\n", "h1") is None
    assert (
        probe.parse_tick_answer(tick_text("success") + "@@failed=system\n", "h1")
        is None
    )


def test_pool_tick_wiring_wired_when_recent_and_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "pool_hosts", lambda _path: {"user@h1": "h1"})
    now = datetime(2026, 11, 14, 8, 30, 0, tzinfo=UTC)
    verdict = probe.pool_tick_wiring(
        Tick("census", 3600.0),
        Wiring(),
        now,
        "p",
        ask=lambda _target, _name: tick_text("success"),
    )
    assert verdict.wired is True
    assert "runs on h1" in verdict.detail
    assert verdict.ran_at == "2026-11-14T08:00:00Z"


def test_pool_tick_wiring_failed_service_is_not_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "pool_hosts", lambda _path: {"user@h1": "h1"})
    now = datetime(2026, 11, 14, 8, 30, 0, tzinfo=UTC)
    verdict = probe.pool_tick_wiring(
        Tick("census", 3600.0),
        Wiring(),
        now,
        "p",
        ask=lambda _target, _name: tick_text("exit-code"),
    )
    assert verdict.wired is False
    assert "failed its last run" in verdict.detail


def test_pool_tick_wiring_stale_timer_keeps_the_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "pool_hosts", lambda _path: {"user@h1": "h1"})
    now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
    verdict = probe.pool_tick_wiring(
        Tick("census", 3600.0),
        Wiring(),
        now,
        "p",
        ask=lambda _target, _name: tick_text("success"),
    )
    assert verdict.wired is False
    assert "older than twice" in verdict.detail


def test_pool_tick_wiring_returns_judged_verdicts_unchanged() -> None:
    wired = Wiring(wired=True)
    assert probe.pool_tick_wiring(Tick("census", 3600.0), wired, None, "p") is wired
    unjudgeable = Wiring(unjudgeable=True)
    assert (
        probe.pool_tick_wiring(Tick("census", 3600.0), unjudgeable, None, "p")
        is unjudgeable
    )
    missing = Wiring()
    assert probe.pool_tick_wiring(Tick("census", 0.0), missing, None, "p") is missing
