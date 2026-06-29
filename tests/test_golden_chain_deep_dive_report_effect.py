# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_deep_dive_report_effect (OMN-13725).

The EFFECT is exercised with a **mock** ``ProtocolReportDataSource`` injected —
no real git/``gh`` is touched.  One case round-trips the emitted result event
through ``EventBusInmemory`` to prove the report survives bus transit.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_deep_dive_report_effect.deep_dive import (
    ActiveWorktree,
    CommitEntry,
    DriftReport,
    GitHubMergedPR,
    RepoDay,
)
from omnimarket.nodes.node_deep_dive_report_effect.handlers.handler_deep_dive_report_effect import (
    HandlerDeepDiveReportEffect,
)
from omnimarket.nodes.node_deep_dive_report_effect.models.model_deep_dive_report_io import (
    ModelDeepDiveReportCommand,
    ModelDeepDiveReportResultEvent,
)

pytestmark = pytest.mark.unit

_DATE = dt.date(2026, 6, 28)


def _commit(full: str, subject: str) -> CommitEntry:
    return CommitEntry(
        full=full,
        short=full[:8],
        ai="2026-06-28 10:00:00 +0000",
        author="agent",
        subject=subject,
        files=2,
        ins=40,
        dele=5,
    )


def _pr(
    number: int, title: str, category: str, *, exempt: bool = False
) -> GitHubMergedPR:
    return GitHubMergedPR(
        number=number,
        title=title,
        merged_at="10:00",
        is_workflow_pr=False,
        category=category,
        is_exempt=exempt,
        additions=120,
        deletions=30,
    )


class MockReportDataSource:
    """Deterministic, no-I/O ``ProtocolReportDataSource`` for golden tests."""

    def __init__(
        self,
        repo_days: list[RepoDay],
        drift: DriftReport,
        *,
        forbid_reads: bool = False,
    ) -> None:
        self._repo_days = repo_days
        self._drift = drift
        self._forbid_reads = forbid_reads
        self.calls: list[str] = []

    def resolve_date(self, date_str: str | None) -> dt.date:
        self.calls.append("resolve_date")
        return dt.date.fromisoformat(date_str) if date_str else _DATE

    def discover_repos(self, root: Path, prefixes: tuple[str, ...]) -> list[Path]:
        if self._forbid_reads:
            raise AssertionError("discover_repos must not run (dry_run / no reads)")
        self.calls.append("discover_repos")
        return [rd.path for rd in self._repo_days]

    def scan_repo_day(
        self, repo: Path, date: dt.date, *, include_dirty: bool
    ) -> RepoDay | None:
        if self._forbid_reads:
            raise AssertionError("scan_repo_day must not run")
        self.calls.append("scan_repo_day")
        for rd in self._repo_days:
            if rd.path == repo:
                return rd
        return None

    def active_worktrees(self, repo: Path, repo_name: str) -> list[ActiveWorktree]:
        self.calls.append("active_worktrees")
        return []

    def compute_drift(
        self, repo_days: list[RepoDay], root: Path, date: dt.date
    ) -> DriftReport:
        if self._forbid_reads:
            raise AssertionError("compute_drift must not run")
        self.calls.append("compute_drift")
        return self._drift


def _active_repo_days() -> list[RepoDay]:
    return [
        RepoDay(
            name="omnibase_core",
            path=Path("/ws/omnibase_core"),
            branch="main",
            commits=[
                _commit("a" * 40, "feat(OMN-1): add runtime dispatch wiring"),
                _commit("b" * 40, "docs: update handoff"),
            ],
            merges=[],
            dirty=[],
            github_merged_prs=[
                _pr(1, "feat(OMN-1): add runtime dispatch wiring", "capability"),
                _pr(2, "feat: no ticket here", "capability"),
            ],
        )
    ]


def _green_drift() -> DriftReport:
    return DriftReport(
        level="green",
        main_dirty=0,
        stale_branches=0,
        diverged_branches=0,
        risks=[],
        penalty=0,
        active_worktrees=0,
        unlinked_pr_count=1,
        total_pr_count=2,
    )


@pytest.mark.asyncio
async def test_report_with_activity_emits_markdown_and_metrics() -> None:
    ds = MockReportDataSource(_active_repo_days(), _green_drift())
    handler = HandlerDeepDiveReportEffect(data_source=ds)
    cmd = ModelDeepDiveReportCommand(correlation_id=uuid4(), root="/ws")

    out = await handler.handle(cmd)

    assert len(out.events) == 1
    event = out.events[0]
    assert isinstance(event, ModelDeepDiveReportResultEvent)
    assert event.quiet_day is False
    assert event.report_date == "2026-06-28"
    assert event.metrics.total_prs == 2
    assert event.metrics.unlinked_pr_count == 1
    assert event.metrics.drift_level == "green"
    assert event.metrics.category_counts == {"capability": 2}
    assert event.metrics.effectiveness_score > 0
    assert event.metrics.velocity_score > 0
    assert "OMN-1" in event.metrics.ticket_ids
    assert "## Scorecard" in event.report_markdown
    # The mock recorded the data-source calls — all I/O routed through the adapter.
    assert "compute_drift" in ds.calls
    assert "scan_repo_day" in ds.calls


@pytest.mark.asyncio
async def test_quiet_day_still_emits_payload() -> None:
    ds = MockReportDataSource([], _green_drift())
    handler = HandlerDeepDiveReportEffect(data_source=ds)
    cmd = ModelDeepDiveReportCommand(correlation_id=uuid4(), root="/ws")

    out = await handler.handle(cmd)
    event = out.events[0]
    assert isinstance(event, ModelDeepDiveReportResultEvent)
    assert event.quiet_day is True
    assert "Quiet day" in event.report_markdown
    assert event.metrics.total_prs == 0


@pytest.mark.asyncio
async def test_dry_run_performs_no_reads() -> None:
    ds = MockReportDataSource([], _green_drift(), forbid_reads=True)
    handler = HandlerDeepDiveReportEffect(data_source=ds)
    cmd = ModelDeepDiveReportCommand(correlation_id=uuid4(), root="/ws", dry_run=True)

    out = await handler.handle(cmd)  # must not raise (no reads attempted)
    event = out.events[0]
    assert isinstance(event, ModelDeepDiveReportResultEvent)
    assert event.quiet_day is True
    assert "discover_repos" not in ds.calls


@pytest.mark.asyncio
async def test_result_survives_eventbus_inmemory_transit() -> None:
    ds = MockReportDataSource(_active_repo_days(), _green_drift())
    handler = HandlerDeepDiveReportEffect(data_source=ds)
    cmd = ModelDeepDiveReportCommand(correlation_id=uuid4(), root="/ws")
    out = await handler.handle(cmd)
    event = out.events[0]
    assert isinstance(event, ModelDeepDiveReportResultEvent)

    bus = EventBusInmemory()
    await bus.start()
    received: list[ModelDeepDiveReportResultEvent] = []

    async def _on_message(message: object) -> None:
        payload = json.loads(message.value.decode("utf-8"))  # type: ignore[attr-defined]
        received.append(ModelDeepDiveReportResultEvent.model_validate(payload))

    topic = "onex.evt.omnimarket.deep-dive-report-completed.v1"
    await bus.subscribe(topic, on_message=_on_message, group_id="test.deep_dive")
    await bus.publish(
        topic,
        key=None,
        value=event.model_dump_json().encode("utf-8"),
    )
    await bus.shutdown()

    assert len(received) == 1
    assert received[0].metrics.total_prs == 2
    assert received[0].correlation_id == event.correlation_id
