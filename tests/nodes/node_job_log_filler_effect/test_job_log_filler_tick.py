# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The job-log filler runs on a timer and fills newest failure first (OMN-23032).

Measured 2026-11-09 against the live PR watcher state: 844 merge-relevant runs, a per-pass cap of 20, no timer
unit declared for ``onex job-log-fill`` anywhere and no overlay for the node on any overlay root, so the gateway's
cache held no log of the red jobs lanes had to read (`JOB-LOG-CACHE-MISS`). These tests pin the three repairs:

* a backlog of old reds never holds a fresh failure behind the per-pass cap (the failing control: read in watcher
  order, the fresh job is deferred and its log is not cached);
* one timer tick runs a pass and writes the reading the floor reads, whatever the pass ends as;
* the host installer declares the unit that runs the tick, on the PR watcher's host, every two minutes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_job_log_filler_effect import (
    runtime_job_log_filler_tick as tick,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    ModelJobLogFillRequest,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    make_checkout,
    plist_of,
    real_run,
    request,
)
from tests.nodes.node_job_log_filler_effect.planted import (
    RED_JOB,
    SUMMARY_JOB,
    _record,
    overlay,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit

FRESH_JOB = 128000000099
NOW = datetime(2026, 11, 9, 8, 30, tzinfo=UTC)
ENTRY = "src/omnimarket/nodes/node_job_log_filler_effect/runtime_job_log_filler_tick.py"


def backlog_state() -> dict[str, Any]:
    """Ten pull requests that went red yesterday, then one that went red a minute ago, last in the file."""
    prs: dict[str, Any] = {}
    for n in range(10, 20):
        prs[f"omnimarket#{n}"] = _record(
            n,
            "red",
            red=["Tests Gate"],
            detail=[["Tests Gate", str(128000000100 + n), "2026-11-08T10:00:00Z"]],
        )
    prs["omnimarket#20"] = _record(
        20,
        "red",
        red=["Tests Gate"],
        detail=[["Tests Gate", str(FRESH_JOB), "2026-11-09T08:29:00Z"]],
    )
    return {"last_tick": "2026-11-09T08:29:30Z", "prs": prs}


def handler_for(
    tmp_path: Path, transport: Any, state: dict[str, Any], **settings: Any
) -> HandlerJobLogFiller:
    return HandlerJobLogFiller(
        overlay=overlay(**settings),
        reader_factory=lambda _profile: InMemoryJobLogReader(
            tmp_path / "job-logs",
            ttl_seconds=86400,
            tail_bytes=4096,
            transport_factory=lambda: transport,
        ),
        state_reader=lambda: state,
    )


# ------------------------------------------------ newest failure first, under the cap


def test_a_fresh_failure_is_filled_in_the_pass_after_it_appears_behind_a_backlog(
    tmp_path: Path,
) -> None:
    transport = transport_for(FRESH_JOB)  # the only GitHub read the cap of one allows
    handler = handler_for(tmp_path, transport, backlog_state(), max_reads_per_pass=1)
    result = handler.fill(ModelJobLogFillRequest())
    filled = [i for i in result.items if i.outcome is EnumJobLogFillOutcome.FILLED]
    assert [i.run.job_id for i in filled] == [FRESH_JOB]
    assert (result.filled, result.deferred, result.failed) == (1, 10, 0)
    transport.assert_drained()


def test_red_runs_are_read_before_named_ones_and_newer_before_older(
    tmp_path: Path,
) -> None:
    state: Any = watcher_state()
    # a second red job on the planted pull request, completed after the first one
    state["prs"]["omnimarket#1"]["ci"]["red"] = ["Tests Gate", "Lint"]
    state["prs"]["omnimarket#1"]["ci"]["detail"].append(
        ["Lint", "128000000077", "2026-11-07T22:30:00Z"]
    )
    handler = handler_for(
        tmp_path, transport_for(128000000077, RED_JOB, SUMMARY_JOB), state
    )
    result = handler.fill(ModelJobLogFillRequest())
    assert [i.run.job_id for i in result.items] == [128000000077, RED_JOB, SUMMARY_JOB]


# ------------------------------------------------ a log already confirmed is not probed again


class CountingReader:
    """A reader over the real in-memory cache that counts the cache-only probes it is asked."""

    def __init__(self, inner: InMemoryJobLogReader) -> None:
        self.inner = inner
        self.cache_path = inner.cache_path
        self.probes: list[int] = []

    def read(self, repo: str, job_id: int, *, cache_only: bool) -> Any:
        if cache_only:
            self.probes.append(job_id)
        return self.inner.read(repo, job_id, cache_only=cache_only)


def counting_handler(
    tmp_path: Path, transport: Any, held: dict[int, float], clock: Any
) -> tuple[HandlerJobLogFiller, CountingReader]:
    reader = CountingReader(
        InMemoryJobLogReader(
            tmp_path / "job-logs",
            ttl_seconds=86400,
            tail_bytes=4096,
            transport_factory=lambda: transport,
        )
    )
    return (
        HandlerJobLogFiller(
            overlay=overlay(reprobe_seconds=600),
            reader_factory=lambda _profile: reader,
            state_reader=watcher_state,
            held=held,
            clock=clock,
        ),
        reader,
    )


def test_a_confirmed_log_is_not_probed_again_until_the_reprobe_window_passes(
    tmp_path: Path,
) -> None:
    held: dict[int, float] = {}
    clock = [1000.0]
    handler, reader = counting_handler(
        tmp_path, transport_for(RED_JOB, SUMMARY_JOB), held, lambda: clock[0]
    )
    handler.fill(ModelJobLogFillRequest())
    assert reader.probes == [
        RED_JOB,
        SUMMARY_JOB,
    ]  # the first pass probes both, then fills
    assert set(held) == {RED_JOB, SUMMARY_JOB}
    reader.probes.clear()
    clock[0] += 599
    second = handler.fill(ModelJobLogFillRequest())
    assert reader.probes == []  # inside the window nothing is asked of the cache
    assert (second.filled, second.already_cached, second.failed) == (0, 2, 0)
    clock[0] += 2
    handler.fill(ModelJobLogFillRequest())
    assert reader.probes == [RED_JOB, SUMMARY_JOB]  # past it, each is probed once more


def test_control_a_cache_that_lost_its_log_is_refilled_once_the_window_passes(
    tmp_path: Path,
) -> None:
    held: dict[int, float] = {}
    clock = [1000.0]
    handler, _ = counting_handler(
        tmp_path, transport_for(RED_JOB, SUMMARY_JOB, RED_JOB), held, lambda: clock[0]
    )
    handler.fill(ModelJobLogFillRequest())
    for entry in (tmp_path / "job-logs").iterdir():  # the gateway's cache is wiped
        if entry.suffix in {".log", ".json"}:
            entry.unlink()
    clock[0] += 601
    third = handler.fill(ModelJobLogFillRequest())
    assert RED_JOB in {
        i.run.job_id for i in third.items if i.outcome is EnumJobLogFillOutcome.FILLED
    }


def test_a_job_no_longer_in_play_and_a_failed_read_leave_the_memo(
    tmp_path: Path,
) -> None:
    held: dict[int, float] = {
        999: 1.0,
        SUMMARY_JOB: 1.0,
    }  # 999 is on no open pull request
    transport = transport_for(
        RED_JOB, SUMMARY_JOB, bodies={SUMMARY_JOB: (404, b'{"message":"Not Found"}')}
    )
    handler, _ = counting_handler(tmp_path, transport, held, lambda: 5000.0)
    handler.fill(ModelJobLogFillRequest())
    assert held == {RED_JOB: 5000.0}


# --------------------------------------------------------------------- one timer tick


def run_tick(tmp_path: Path, handler: HandlerJobLogFiller | None) -> tuple[int, Path]:
    state_dir = tmp_path / "evidence"
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(state_dir)],
        handler=handler,
        now=NOW,
    )
    return code, state_dir / tick.EVIDENCE_FILE


def test_golden_a_tick_fills_the_cache_and_writes_the_reading(tmp_path: Path) -> None:
    handler = handler_for(
        tmp_path, transport_for(RED_JOB, SUMMARY_JOB), watcher_state()
    )
    code, evidence = run_tick(tmp_path, handler)
    reading = json.loads(evidence.read_text(encoding="utf-8"))
    assert code == 0
    assert reading["status"] == "completed"
    assert reading["ran_at"] == "2026-11-09T08:30:00Z"
    assert (reading["considered"], reading["filled"], reading["failed"]) == (2, 2, 0)
    assert reading["coverage"] == 1.0
    # the next tick finds both logs held and reads nothing from GitHub
    code, evidence = run_tick(
        tmp_path, handler_for(tmp_path, transport_for(), watcher_state())
    )
    assert code == 0
    assert json.loads(evidence.read_text(encoding="utf-8"))["already_cached"] == 2


def test_a_log_github_no_longer_serves_shows_in_the_reading_and_does_not_fail_the_tick(
    tmp_path: Path,
) -> None:
    transport = transport_for(
        RED_JOB, SUMMARY_JOB, bodies={SUMMARY_JOB: (404, b'{"message":"Not Found"}')}
    )
    code, evidence = run_tick(
        tmp_path,
        handler_for(tmp_path, transport, watcher_state(), reader_concurrency=1),
    )
    reading = json.loads(evidence.read_text(encoding="utf-8"))
    assert code == 0
    assert (reading["status"], reading["filled"], reading["failed"]) == (
        "partial",
        1,
        1,
    )
    assert reading["coverage"] == 0.5


def test_a_real_tick_keeps_its_memo_between_ticks_in_the_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = transport_for(RED_JOB, SUMMARY_JOB)
    reader = CountingReader(
        InMemoryJobLogReader(
            tmp_path / "job-logs",
            ttl_seconds=86400,
            tail_bytes=4096,
            transport_factory=lambda: transport,
        )
    )
    real = HandlerJobLogFiller

    def build(overlay: Any = None, held: Any = None, **_: Any) -> HandlerJobLogFiller:
        return real(
            overlay=overlay,
            reader_factory=lambda _p: reader,
            state_reader=watcher_state,
            held=held,
        )

    monkeypatch.setattr(tick, "load_overlay", lambda: overlay())
    monkeypatch.setattr(tick, "HandlerJobLogFiller", build)
    code, _ = run_tick(tmp_path, None)
    assert code == 0
    memo = json.loads((tmp_path / "evidence" / tick.HELD_FILE).read_text())
    assert set(memo) == {str(RED_JOB), str(SUMMARY_JOB)}
    reader.probes.clear()
    code, evidence = run_tick(tmp_path, None)  # a new process reads the memo back
    assert code == 0
    assert reader.probes == []
    assert json.loads(evidence.read_text())["already_cached"] == 2


def test_an_unreadable_memo_is_an_empty_one_and_not_a_failed_tick(
    tmp_path: Path,
) -> None:
    memo = tmp_path / "held.json"
    memo.write_text("{not json", encoding="utf-8")
    assert tick.read_held(memo) == {}
    assert tick.read_held(tmp_path / "absent.json") == {}


def test_error_a_tick_that_cannot_read_the_watcher_state_fails_and_says_why(
    tmp_path: Path,
) -> None:
    def unreadable() -> dict[str, Any]:
        raise ValueError("watcher state unreadable")

    handler = HandlerJobLogFiller(
        overlay=overlay(), reader_factory=lambda _p: None, state_reader=unreadable
    )
    code, evidence = run_tick(tmp_path, handler)
    reading = json.loads(evidence.read_text(encoding="utf-8"))
    assert code == tick.EXIT_FAILED
    assert reading["status"] == "failed"
    assert "watcher state unreadable" in reading["error"]


def test_error_a_host_with_no_overlay_exits_2_and_writes_that_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> None:
        raise NodeOverlayError("no overlay for node_job_log_filler_effect")

    monkeypatch.setattr(tick, "load_overlay", refuse)
    code, evidence = run_tick(tmp_path, None)
    reading = json.loads(evidence.read_text(encoding="utf-8"))
    assert code == tick.EXIT_OVERLAY
    assert reading["status"] == "overlay_unreadable"
    assert "no overlay" in reading["error"]


# ------------------------------------------------------- the unit that runs the tick


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def test_the_installer_declares_a_unit_that_runs_the_tick_every_two_minutes(
    tmp_path: Path,
) -> None:
    declared = load_units()["job-log-filler"]
    assert declared["platform"] == "launchd"
    assert declared["schedule"]["start_interval"] == 120
    assert declared["module"].endswith(".runtime_job_log_filler_tick")
    assert declared["entry_file"] == ENTRY
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit="job-log-filler",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.job-log-filler"
    assert plist["StartInterval"] == 120
    assert declared["module"] in plist["ProgramArguments"]
    environment = plist["EnvironmentVariables"]
    assert environment["ONEX_PR_WATCHER_STATE"] == str(
        tmp_path / "omni" / ".claude_scratch" / "pr-watcher" / "state.json"
    )
    assert environment["ONEX_SKILL_OVERLAY_ROOTS"].endswith(
        "plugins/onex-overlays/overlays"
    )


def test_the_rendered_unit_carries_the_workspace_config_root_the_host_gives(
    tmp_path: Path,
) -> None:
    """Without it every read fails: the gateway cannot find the lane declaration table (OMN-23303)."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit="job-log-filler",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    environment = plist_of(result.rendered)["EnvironmentVariables"]
    assert environment["ONEX_WORKSPACE_CONFIG_ROOT"] == str(tmp_path / "ws")


def test_the_unit_declares_the_workspace_config_root_as_a_host_placeholder() -> None:
    declared = load_units()["job-log-filler"]["environment"]
    assert declared["ONEX_WORKSPACE_CONFIG_ROOT"] == "{workspace_config_root}"


def test_control_the_workspace_config_root_follows_the_host_value(
    tmp_path: Path,
) -> None:
    """A second host root renders a second value, so the line is read from the request and is no constant."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    other = tmp_path / "other-ws"
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit="job-log-filler",
            project_path=str(branch),
            expected_revision=revision,
            workspace_config_root=str(other),
        )
    )
    assert result.status == "success", result.stderr
    environment = plist_of(result.rendered)["EnvironmentVariables"]
    assert environment["ONEX_WORKSPACE_CONFIG_ROOT"] == str(other)


def test_a_job_log_filler_unit_with_no_host_workspace_config_root_is_refused(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit="job-log-filler",
            project_path=str(branch),
            expected_revision=revision,
            workspace_config_root="",
        )
    )
    assert result.status == "failure"
    assert "workspace_config_root" in result.stderr
