# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the dev deploy agent's job records of 2026-11-10 through the batching decision.

The fixture is the read-only summary of ``jobs-dev`` on the dev host (requests whose
``requested_at`` is from 09:30Z on), taken at about 12:13Z. Each request is read as one
``dev_tip_changed`` event: repository from ``requested_by`` (``gha/<repo>/pr-N``), time
from ``requested_at`` (when the CI job published the request, the closest record to the
merge), sha from the request's ``git_ref`` (the record has no merged sha). The replay
steps a simulated clock every ``TICK_SECONDS``; a roll the node decides is simulated to
run for the median duration of the real rolls (accepted to completed, superseded jobs
excluded) and to be healthy from its end. It sends nothing: shadow only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import median
from uuid import UUID, uuid5

import pytest

from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollAction,
    EnumDevRollHealth,
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollPolicy,
    ModelDevRollRecord,
    ModelDevTipChanged,
)

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "dev_jobs_2026_11_10.json"
_NS = UUID("6f1c2f1e-0d1a-4c52-9a53-0a7c5d1b9d10")
_START = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
_END_REQUESTS = datetime(2026, 11, 10, 11, 30, tzinfo=UTC)
_TICK_SECONDS = 60
# The plan's option (b) numbers and the lane's healthy-for proposal, as in the overlay.
_POLICY = ModelDevRollPolicy(
    mode="shadow",
    batch_window_seconds=1800,
    min_start_interval_seconds=1800,
    healthy_for_seconds=600,
    failed_roll_release_after_seconds=3600,
    known_repositories=("omnimarket", "omnibase_infra"),
    build_context_repository="omnibase_infra",
    fallback_git_ref="origin/dev",
    scope="full",
    runtime_lane="dev",
    requested_by="dev-roll-batch/shadow",
)


def _t(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text.replace("Z", "+00:00"))


def _jobs() -> list[dict[str, object]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _events(jobs: list[dict[str, object]]) -> list[ModelDevTipChanged]:
    events = []
    for job in jobs:
        requested_at = _t(str(job["requested_at"]))
        assert requested_at is not None
        if not _START <= requested_at < _END_REQUESTS:
            continue
        events.append(
            ModelDevTipChanged(
                event_id=uuid5(_NS, str(job["job"])),
                repository=str(job["requested_by"]).split("/")[1],
                sha=str(job["git_ref"]),
                merged_at=requested_at,
            )
        )
    return sorted(events, key=lambda e: e.merged_at)


def _real_rolls(jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        j
        for j in jobs
        if j["status"] != "superseded"
        and (_t(str(j["accepted_at"])) or _START) >= _START
        and (_t(str(j["accepted_at"])) or _START) < _END_REQUESTS
    ]


def replay(
    jobs: list[dict[str, object]], policy: ModelDevRollPolicy
) -> list[ModelDevRollDecided]:
    """The ROLL_NOW decisions the node would have made, one per simulated roll."""
    real = [j for j in jobs if j["status"] != "superseded" and j["completed_at"]]
    duration = timedelta(
        seconds=median(
            (_t(str(j["completed_at"])) - _t(str(j["accepted_at"]))).total_seconds()
            for j in real
        )
    )
    pending = _events(jobs)
    running: tuple[str, datetime, datetime] | None = None  # (git_ref, start, end)
    rolls: list[ModelDevRollDecided] = []
    now = _START
    horizon = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    while now <= horizon:
        visible = tuple(e for e in pending if e.merged_at <= now)
        last: ModelDevRollRecord | None = None
        if running is not None:
            ref, started, ended = running
            done = now >= ended  # the record as the reader would see it at `now`
            last = ModelDevRollRecord(
                job_id=str(len(rolls)),
                git_ref=ref,
                runtime_started_at=started,
                finished_at=ended if done else None,
                health=EnumDevRollHealth.HEALTHY if done else EnumDevRollHealth.UNKNOWN,
                healthy_since=ended if done else None,
            )
        facts = ModelDevRollFacts(
            correlation_id=uuid5(_NS, now.isoformat()),
            observed_at=now,
            pending_events=visible,
            last_roll=last,
        )
        decision = decide_dev_roll(facts, policy)
        assert isinstance(decision, ModelDevRollDecided)
        if decision.action is EnumDevRollAction.ROLL_NOW:
            rolls.append(decision)
            covered = set(decision.rolled_event_ids)
            pending = [e for e in pending if e.event_id not in covered]
            assert decision.would_request is not None
            running = (decision.would_request.git_ref, now, now + duration)
        now += timedelta(seconds=_TICK_SECONDS)
    return rolls


def test_the_fixture_holds_the_five_real_rolls_of_the_window() -> None:
    jobs = _jobs()
    assert len(_events(jobs)) == 22
    assert len(_real_rolls(jobs)) == 5


def test_replay_makes_fewer_rolls_than_the_five_real_ones() -> None:
    rolls = replay(_jobs(), _POLICY)
    covered = {i for r in rolls for i in r.rolled_event_ids}
    assert len(covered) == 22, "every tip event is rolled once, none dropped"
    assert len(rolls) < len(_real_rolls(_jobs()))
    # Roll times are at least the minimum start interval apart.
    starts = [r.observed_at for r in rolls]
    gaps = [(b - a).total_seconds() for a, b in pairwise(starts)]
    assert all(g >= _POLICY.min_start_interval_seconds for g in gaps)


def test_replay_reports_the_counts_and_the_longest_wait() -> None:
    rolls = replay(_jobs(), _POLICY)
    longest = max(r.longest_wait_seconds for r in rolls)
    print(
        f"REPLAY would_roll={len(rolls)} real_rolls={len(_real_rolls(_jobs()))} "
        f"longest_wait_seconds={longest} "
        f"rolls_at={[r.observed_at.strftime('%H:%M:%S') for r in rolls]}"
    )
    assert longest > 0
