# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's deploy agent row (OMN-22605): repeated failed deploys read red, an unreadable agent reads unread.

The failed answers below are the shape the dev deploy agent served on 2026-11-06 while every deploy job refused at its
estimator settings check: ``/health`` with ``last_result.status == failed`` and ``/job/<id>`` with ``errors``. The old
floor drew no row for it: its deploy lag row read WORKING at 773 minutes against a 1440-minute limit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.models.floor_17a import (
    ModelLabSourceDef,
    ModelLabStateInput,
    ModelLabStateOverlay,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import read_deploy_lag
from omnimarket.nodes.node_floor_simple_effect.handlers.sources_deploy_agent import (
    deploy_agent_events,
    read_deploy_agent,
    record_deploy_agent,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 10, 40, tzinfo=UTC)
URL = "http://deploy-agent.test:8098"


class _Overlay:
    sources = (
        ModelLabSourceDef(
            id="deploy_agent",
            title="Dev deploy agent",
            kind="deploy_agent",
            max_age_s=900,
            limit=2,
            unit="failed deploys in a row",
        ),
        ModelLabSourceDef(
            id="deploy_lag",
            title="Deploy lag",
            kind="deploy_lag",
            max_age_s=900,
            limit=1440,
            unit="minutes",
        ),
    )
    deploy_agent_url = URL
    deploy_agent_history = 5
    deploy_agent_timeout_s = 1
    deploy_lag_lookback_days = 2


OVERLAY = cast(ModelLabStateOverlay, _Overlay())


class _Agent:
    """The agent's answers, one job at a time; ``down`` answers nothing."""

    def __init__(self) -> None:
        self.health: dict[str, Any] | None = None
        self.jobs: dict[str, dict[str, Any]] = {}

    def finished(self, cid: str, status: str, at: datetime, error: str = "") -> None:
        self.health = {
            "state": "idle",
            "last_result": {
                "correlation_id": cid,
                "status": status,
                "completed_at": at.isoformat(),
            },
        }
        self.jobs[cid] = {
            "status": status,
            "errors": [error] if error else [],
            "phase_results": {"preflight": "success", "runtime": "skipped"},
        }

    def fetch(self, url: str) -> Mapping[str, Any] | None:
        if self.health is None:
            return None
        if url == f"{URL}/health":
            return self.health
        return self.jobs.get(url.rsplit("/", 1)[1])


ESTIMATOR = "RuntimeError: dev_lane_build_settings refuses the build: the estimator settings are missing"


def _cycle(path: Path, agent: _Agent, at: datetime) -> None:
    record_deploy_agent(path, OVERLAY, agent.fetch, at)


def test_golden_a_deploy_that_succeeded_reads_ok_and_a_second_failure_reads_red(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    agent.finished("job-1", "success", NOW - timedelta(hours=2))
    _cycle(path, agent, NOW - timedelta(minutes=30))
    agent.finished("job-2", "failed", NOW - timedelta(minutes=25), ESTIMATOR)
    _cycle(path, agent, NOW - timedelta(minutes=20))
    one = read_deploy_agent(path, OVERLAY, NOW)
    assert one.status == "read"
    assert one.subjects[0].ok
    assert one.subjects[0].value == 1.0
    agent.finished("job-3", "failed", NOW - timedelta(minutes=2), ESTIMATOR)
    _cycle(path, agent, NOW - timedelta(minutes=1))
    two = read_deploy_agent(path, OVERLAY, NOW)
    assert not two.subjects[0].ok
    note = two.subjects[0].note
    assert "2 finished jobs in a row failed (red at 2)" in note
    assert "last success" in note
    assert "estimator settings are missing" in note
    assert [e.phase for e in deploy_agent_events(two, 17, NOW, "c1")] == [
        "start",
        "failure",
    ]


def test_golden_a_success_after_failures_clears_the_row(tmp_path: Path) -> None:
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    for n in range(3):
        agent.finished(f"bad-{n}", "failed", NOW - timedelta(minutes=30 - n), ESTIMATOR)
        _cycle(path, agent, NOW - timedelta(minutes=29 - n))
    assert not read_deploy_agent(path, OVERLAY, NOW).subjects[0].ok
    agent.finished("good", "success", NOW - timedelta(minutes=5))
    _cycle(path, agent, NOW - timedelta(minutes=4))
    after = read_deploy_agent(path, OVERLAY, NOW)
    assert after.subjects[0].ok
    assert [e.phase for e in deploy_agent_events(after, 17, NOW, "c2")] == [
        "start",
        "success",
    ]


def test_error_an_agent_that_does_not_answer_reads_unread_and_keeps_what_was_seen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    agent.finished("job-1", "failed", NOW - timedelta(minutes=9), ESTIMATOR)
    _cycle(path, agent, NOW - timedelta(minutes=8))
    agent.health = None
    _cycle(path, agent, NOW - timedelta(minutes=1))
    read = read_deploy_agent(path, OVERLAY, NOW)
    assert read.status == "unread"
    assert "did not answer" in read.reason
    events = deploy_agent_events(read, 17, NOW, "c3")
    assert events[1].failure_code == "deploy_agent_unread"
    agent.finished("job-2", "failed", NOW, ESTIMATOR)
    _cycle(path, agent, NOW)
    assert read_deploy_agent(path, OVERLAY, NOW).subjects[0].value == 2.0


def test_error_a_missing_or_garbled_record_is_unread_never_blank(
    tmp_path: Path,
) -> None:
    assert read_deploy_agent(tmp_path / "absent.json", OVERLAY, NOW).status == "unread"
    garbled = tmp_path / "garbled.json"
    garbled.write_text("{not json", encoding="utf-8")
    assert read_deploy_agent(garbled, OVERLAY, NOW).status == "unread"


def test_control_the_old_floor_reads_working_on_the_same_failures(
    tmp_path: Path,
) -> None:
    """Without the deploy agent row the lab state is WORKING: the 773-minute merge is inside the 1440-minute limit."""
    merged_at = (NOW - timedelta(minutes=773)).isoformat()
    record = {
        "floor": {
            "merged_running": {
                "generated_at": NOW.isoformat(),
                "days": [
                    {
                        "merges": [
                            {
                                "repo": "omnimarket",
                                "number": 5140,
                                "merged_at": merged_at,
                                "running": False,
                                "reason": "not_yet_deployed",
                            }
                        ]
                    }
                ],
            }
        }
    }
    lag = read_deploy_lag(record, NOW, OVERLAY)
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    for n in range(3):
        agent.finished(f"bad-{n}", "failed", NOW - timedelta(minutes=20 - n), ESTIMATOR)
        _cycle(path, agent, NOW - timedelta(minutes=19 - n))
    agent_read = read_deploy_agent(path, OVERLAY, NOW)
    sources = OVERLAY.sources
    old = HandlerLabState().handle(
        ModelLabStateInput(now=NOW, sources=sources[1:], reads=(lag,))
    )
    new = HandlerLabState().handle(
        ModelLabStateInput(now=NOW, sources=sources, reads=(agent_read, lag))
    )
    assert {v.id: v.state for v in old.verdicts} == {"deploy_lag": "WORKING"}
    states = {v.id: v.state for v in new.verdicts}
    assert states["deploy_agent"] == "NOT WORKING"
    assert states["deploy_lag"] == "WORKING"


def test_golden_jobs_the_agent_superseded_are_neither_failures_nor_successes(
    tmp_path: Path,
) -> None:
    """The shape on 2026-11-06: a success, a failure, then the agent ended four queued jobs as superseded within one
    second. The row stays at one failure (red at 2) and the superseded jobs take no place in the bounded record."""
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    agent.finished("good", "success", NOW - timedelta(minutes=40))
    _cycle(path, agent, NOW - timedelta(minutes=39))
    agent.finished("interrupted", "failed", NOW - timedelta(minutes=30), ESTIMATOR)
    _cycle(path, agent, NOW - timedelta(minutes=29))
    for n in range(4):
        agent.finished(f"superseded-{n}", "superseded", NOW - timedelta(minutes=20))
        _cycle(path, agent, NOW - timedelta(minutes=19 - n))
    read = read_deploy_agent(path, OVERLAY, NOW)
    assert read.status == "read"
    assert read.subjects[0].ok
    assert read.subjects[0].value == 1.0
    assert read.summary == "1 failed in a row, 1 of 2 recorded results failed"
    assert [e.phase for e in deploy_agent_events(read, 17, NOW, "c4")] == [
        "start",
        "success",
    ]


def test_error_failures_stay_red_across_a_superseded_job_and_a_success_after_it_clears(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    for n in range(2):
        agent.finished(f"bad-{n}", "failed", NOW - timedelta(minutes=30 - n), ESTIMATOR)
        _cycle(path, agent, NOW - timedelta(minutes=29 - n))
    agent.finished("replaced", "superseded", NOW - timedelta(minutes=10))
    _cycle(path, agent, NOW - timedelta(minutes=9))
    red = read_deploy_agent(path, OVERLAY, NOW)
    assert not red.subjects[0].ok
    assert red.subjects[0].value == 2.0
    agent.finished("good", "success", NOW - timedelta(minutes=2))
    _cycle(path, agent, NOW - timedelta(minutes=1))
    assert read_deploy_agent(path, OVERLAY, NOW).subjects[0].ok


def test_a_record_written_before_the_change_with_superseded_jobs_is_read_without_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deploy-agent.json"
    path.write_text(
        '{"observed_at": "2026-11-06T10:39:00Z", "agent_readable": true, "error": "", "state": "idle", "jobs": ['
        '{"correlation_id": "a", "status": "success", "completed_at": "2026-11-06T10:00:00Z", "reason": ""},'
        '{"correlation_id": "b", "status": "superseded", "completed_at": "2026-11-06T10:01:00Z", "reason": ""},'
        '{"correlation_id": "c", "status": "superseded", "completed_at": "2026-11-06T10:02:00Z", "reason": ""}]}',
        encoding="utf-8",
    )
    read = read_deploy_agent(path, OVERLAY, NOW)
    assert read.subjects[0].ok
    assert read.subjects[0].value == 0.0


def test_a_legacy_record_full_of_superseded_jobs_does_not_lose_a_failure_to_the_bound(
    tmp_path: Path,
) -> None:
    """A record written before the change: one failure, then four superseded jobs, history bound 5.

    The next failure used to push the first one out of the bound while the superseded entries
    stayed, so two failures with no success between them read as one and the row stayed ok.
    """
    path = tmp_path / "deploy-agent.json"
    legacy = [
        {
            "correlation_id": "bad-0",
            "status": "failed",
            "completed_at": "2026-11-06T10:00:00Z",
            "reason": ESTIMATOR,
        }
    ] + [
        {
            "correlation_id": f"sup-{n}",
            "status": "superseded",
            "completed_at": f"2026-11-06T10:0{n + 1}:00Z",
            "reason": "",
        }
        for n in range(4)
    ]
    path.write_text(
        json.dumps(
            {
                "observed_at": "2026-11-06T10:09:00Z",
                "agent_readable": True,
                "error": "",
                "state": "idle",
                "jobs": legacy,
            }
        ),
        encoding="utf-8",
    )
    agent = _Agent()
    agent.finished("bad-1", "failed", NOW - timedelta(minutes=2), ESTIMATOR)
    _cycle(path, agent, NOW - timedelta(minutes=1))

    read = read_deploy_agent(path, OVERLAY, NOW)
    assert read.subjects[0].value == 2.0
    assert not read.subjects[0].ok


def test_ac2_replay_success_four_superseded_success_reads_zero_failed_in_a_row(
    tmp_path: Path,
) -> None:
    """OMN-22737 AC2 names this sequence exactly (success, four superseded, success); the golden replay above uses another."""
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    agent.finished("good-0", "success", NOW - timedelta(minutes=40))
    _cycle(path, agent, NOW - timedelta(minutes=39))
    for n in range(4):
        agent.finished(f"superseded-{n}", "superseded", NOW - timedelta(minutes=20))
        _cycle(path, agent, NOW - timedelta(minutes=19 - n))
        # Each supersession read on its own: the old row read 1, 2, 3, 4 failed in a row here.
        assert read_deploy_agent(path, OVERLAY, NOW).subjects[0].value == 0.0
    agent.finished("good-1", "success", NOW - timedelta(minutes=5))
    _cycle(path, agent, NOW - timedelta(minutes=4))
    read = read_deploy_agent(path, OVERLAY, NOW)
    assert read.subjects[0].ok
    assert read.subjects[0].value == 0.0
    assert read.summary == "0 failed in a row, 0 of 2 recorded results failed"


def test_the_note_does_not_claim_no_success_when_only_a_bounded_history_is_kept(
    tmp_path: Path,
) -> None:
    """OMN-22737 review finding: a success older than the kept history is not in the record, so the note must say so."""
    path = tmp_path / "deploy-agent.json"
    agent = _Agent()
    agent.finished("good", "success", NOW - timedelta(minutes=60))
    _cycle(path, agent, NOW - timedelta(minutes=59))
    for n in range(OVERLAY.deploy_agent_history):
        agent.finished(f"bad-{n}", "failed", NOW - timedelta(minutes=30 - n), ESTIMATOR)
        _cycle(path, agent, NOW - timedelta(minutes=29 - n))
    note = read_deploy_agent(path, OVERLAY, NOW).subjects[0].note
    assert "no success among the newest" in note
    assert f"keeps only the newest {OVERLAY.deploy_agent_history}" in note


def test_a_merge_read_as_not_running_names_the_unread_running_build() -> None:
    """OMN-23031: the row read 'merged, not running' while the count's own record said it could not read the running build."""
    record = {
        "floor": {
            "merged_running": {
                "generated_at": NOW.isoformat(),
                "evidence_unread": [
                    "deployed: omnibase_infra: history of the deployed ref unreadable (git rev-list exited 128)"
                ],
                "days": [
                    {
                        "merges": [
                            {
                                "repo": "omnibase_infra",
                                "number": 5663,
                                "merged_at": (
                                    NOW - timedelta(minutes=2773)
                                ).isoformat(),
                                "running": False,
                                "reason": "not_yet_deployed",
                            }
                        ]
                    }
                ],
            }
        }
    }
    read = read_deploy_lag(record, NOW, OVERLAY)
    assert "evidence unread: deployed: omnibase_infra: history of the deployed ref" in (
        read.subjects[0].note
    )
    clean = cast(dict[str, Any], record["floor"])["merged_running"]
    del clean["evidence_unread"]
    assert (
        "evidence unread" not in read_deploy_lag(record, NOW, OVERLAY).subjects[0].note
    )
