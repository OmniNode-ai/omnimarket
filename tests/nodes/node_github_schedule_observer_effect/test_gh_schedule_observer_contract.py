# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract, clone-reader, state-store and dead-man checks of the GitHub schedule observer (OMN-20803)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunPhase,
    ModelAutomationHeartbeat,
    automation_liveness_topics,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.git_schedule_clone_reader import (
    GitScheduleCloneReader,
    schedule_crons,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.schedule_observer_state_store import (
    FileScheduleObserverStateStore,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols import (
    ScheduleCloneError,
)
from tests.nodes.node_github_schedule_observer_effect.support import (
    RecordedTransport,
    current_clone,
    events_of,
    handler_for,
    request_at,
    run_events,
    verdict_events,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_github_schedule_observer_effect"
)


def test_gh_schedule_observer_contract_publishes_the_seam_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    seam = automation_liveness_topics()
    published = set(contract["event_bus"]["publish_topics"])
    assert published == {
        seam[EnumAutomationLivenessEvent.RUN_OBSERVED],
        seam[EnumAutomationLivenessEvent.HEARTBEAT],
        seam[EnumAutomationLivenessEvent.LIVENESS_VERDICT],
    }
    assert {e["topic"] for e in contract["published_events"]} == published


def test_gh_schedule_observer_schedule_crons_reads_the_on_key_either_way() -> None:
    quoted = "'on':\n  schedule:\n    - cron: '5 * * * *'\n"
    bare = "on:\n  push: {}\n  schedule:\n    - cron: '0 3 * * 1'\n    - cron: '0 4 * * 1'\n"
    assert schedule_crons(quoted) == ("5 * * * *",)
    assert schedule_crons(bare) == ("0 3 * * 1", "0 4 * * 1")
    assert schedule_crons("on:\n  push: {}\n") == ()
    assert schedule_crons("not: [valid") == ()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(),
    ).stdout.strip()


def test_gh_schedule_observer_git_clone_reader_compares_head_with_remote_default(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "trunk")
    (origin / ".github/workflows").mkdir(parents=True)
    (origin / ".github/workflows/nightly.yml").write_text(
        "on:\n  schedule:\n    - cron: '15 2 * * *'\n", encoding="utf-8"
    )
    (origin / ".github/workflows/ci.yml").write_text("on: push\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(
        origin,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    clone = tmp_path / "clones" / "example-repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(),
    )

    reader = GitScheduleCloneReader()
    current = reader.read_clone(clone)
    assert current.is_current
    assert current.default_branch == "trunk"
    assert [(w.path, w.crons) for w in current.workflows] == [
        (".github/workflows/nightly.yml", ("15 2 * * *",))
    ]

    _git(
        clone,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "local only",
    )
    behind = reader.read_clone(clone)
    assert not behind.is_current, "a clone whose head moved off the remote is stale"

    with pytest.raises(ScheduleCloneError):
        reader.read_clone(tmp_path / "clones" / "absent")


def test_gh_schedule_observer_state_store_refuses_to_reset_a_corrupt_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    store = FileScheduleObserverStateStore(path)
    assert store.load().ticks_completed == 0, "no file yet is an empty state"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not resetting the cursors"):
        store.load()


async def test_gh_schedule_observer_reports_a_scheduled_workflow_with_no_entry(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport("quiet_schedule")

    output = await handler_for(
        transport, current_clone("chain-canary.yml", "Orphan_Job.yml")
    ).handle(request_at("2026-10-10T04:00:00Z", tmp_path))

    (verdict,) = verdict_events(output)
    assert verdict.verdict is EnumAutomationLivenessVerdict.UNDECLARED
    assert verdict.reason is EnumAutomationLivenessReason.PROCESS_WITHOUT_ENTRY
    assert verdict.process_id == "github/example-repo/orphan_job"
    assert verdict.contract_digest is None


async def test_gh_schedule_observer_reads_the_external_deadmans_last_completed_run(
    tmp_path: Path,
) -> None:
    deadman = {
        "process_id": "github/deadman-repo/deadman",
        "repository": "example-owner/deadman-repo",
        "workflow_file": "deadman.yml",
    }
    transport = RecordedTransport("quiet_schedule", "deadman_last_run")
    request = request_at("2026-10-10T04:00:00Z", tmp_path, external_deadman=deadman)

    output = await handler_for(transport, current_clone("chain-canary.yml")).handle(
        request
    )

    (last,) = run_events(output)
    assert last.process_id == "github/deadman-repo/deadman"
    assert last.run_id == "github/deadman-repo/deadman:9001"
    assert last.phase is EnumAutomationRunPhase.FINISHED

    again = RecordedTransport("empty_runs", "deadman_last_run")
    output = await handler_for(again, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T05:00:00Z", tmp_path, external_deadman=deadman)
    )
    assert run_events(output) == [], "the same completed run is not reported twice"


async def test_gh_schedule_observer_refuses_an_undeclared_deadman(
    tmp_path: Path,
) -> None:
    request = request_at(
        "2026-10-10T04:00:00Z",
        tmp_path,
        external_deadman={
            "process_id": "github/nowhere/deadman",
            "repository": "example-owner/deadman-repo",
            "workflow_file": "deadman.yml",
        },
    )
    with pytest.raises(ValueError, match="no overlay entry"):
        await handler_for(RecordedTransport(), current_clone()).handle(request)


async def test_gh_schedule_observer_heartbeat_counts_ticks(tmp_path: Path) -> None:
    observer = {"process_id": "github/observer", "host": "h201"}
    clone = current_clone("chain-canary.yml")
    first = await handler_for(RecordedTransport("quiet_schedule"), clone).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path, observer=observer)
    )
    second = await handler_for(RecordedTransport("empty_runs"), clone).handle(
        request_at("2026-10-10T05:00:00Z", tmp_path, observer=observer)
    )
    (beat1,) = events_of(first, ModelAutomationHeartbeat)
    (beat2,) = events_of(second, ModelAutomationHeartbeat)
    assert (beat1.progress_counter, beat2.progress_counter) == (1, 2)
    assert beat1.process_started_at == beat2.process_started_at
