# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A small synthetic week around the reset, with every source declared and one day unread."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.models.process_health import (
    EnumReadStatus,
    EnumSourceName,
    ModelControllerDecision,
    ModelControllerLease,
    ModelDelegationRow,
    ModelLandingMetricsWindow,
    ModelLinearIssue,
    ModelMergeRecord,
    ModelProcessHealthConfig,
    ModelProcessHealthRequest,
    ModelRepoStatusEvent,
    ModelSourceRead,
    ModelTimeSpan,
)

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
GENERATED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SOURCES_FROM = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
LEDGER_FROM = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026 (UTC)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def contract_config() -> ModelProcessHealthConfig:
    """The config exactly as the node contract declares it."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelProcessHealthConfig.model_validate(contract["config"]["process_health"])


def _row(
    day: int, hour: int, minute: int, rtype: str, text: str
) -> ModelLandingLedgerRow:
    stamp = f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00Z"
    lane = text.split("lane=", 1)[1].split(" | ", 1)[0]
    return ModelLandingLedgerRow(ts=stamp, rtype=rtype, lane=lane, text=text)


def ledger_rows() -> tuple[ModelLandingLedgerRow, ...]:
    """FRICTION in the three cost buckets, lane TERMINALs, a duplicated TERMINAL, an andon HOLD and its RELEASE."""
    return (
        _row(
            28,
            10,
            0,
            "FRICTION",
            "lane=a | actor=claude:sonnet:x | class=gate-conflict | cost=~30 lane-minutes | gates fought",
        ),
        _row(
            28,
            11,
            0,
            "FRICTION",
            "lane=landing-L1 | actor=hook | class=refusal | guard=g1 | reason=foreground-bash-shape | cost=~1 lane-minute | refused",
        ),
        _row(
            28,
            12,
            0,
            "FRICTION",
            "lane=fill | actor=launchd:fill | class=lab-idle | cost=est 140 lane-minutes (free slots x next 20m interval) | idle",
        ),
        _row(
            28,
            13,
            0,
            "FRICTION",
            "lane=h | actor=hook | class=refusal | guard=hook-preflight | reason=preflight | cost=~1 lane-minute | blocked",
        ),
        _row(
            29,
            5,
            0,
            "FRICTION",
            "lane=b | actor=claude:sonnet:y | class=gate-conflict | cost=1 hour | gates fought",
        ),
        _row(
            28,
            14,
            0,
            "TERMINAL",
            "lane=x1 | outcome=done | delegated=1 runs=r1 | closes-CLAIM=2026-10-28T09:00:00Z | runner row",
        ),
        _row(
            28,
            14,
            1,
            "TERMINAL",
            "lane=x1 | outcome=done | delegated=1 runs=r1 | closes-CLAIM=2026-10-28T09:00:00Z | lane row",
        ),
        _row(
            28,
            15,
            0,
            "TERMINAL",
            "lane=x2 | outcome=blocked | delegated=0 | closes-CLAIM=2026-10-28T09:10:00Z | blocked",
        ),
        _row(
            28,
            16,
            0,
            "TERMINAL",
            "lane=landing-L5 | outcome=handed-off | result=blocked | delegated=1 | closes-CLAIM=2026-10-28T09:20:00Z | gated",
        ),
        _row(
            29,
            6,
            0,
            "TERMINAL",
            "lane=y1 | outcome=done | delegated=2 | closes-CLAIM=2026-10-29T02:00:00Z | done",
        ),
        _row(
            29,
            7,
            0,
            "TERMINAL",
            "lane=y2 | outcome=handed-off | delegated=1 | closes-CLAIM=2026-10-29T02:10:00Z | handed off",
        ),
        _row(
            29,
            5,
            30,
            "HOLD",
            "lane=andon-hold | id=2026-10-29T05:30:00Z-andon-hold | to=all | until=2026-11-05T00:00:00Z | reason=andon:gate:0 | stop the line",
        ),
        _row(
            29,
            9,
            30,
            "RELEASE",
            "lane=orch | re=2026-10-29T05:30:00Z-andon-hold | result=lifted | lifted",
        ),
    )


def _merge(
    repo: str,
    number: int,
    when: datetime,
    subject: str,
    author: str = "dev",
    docs: bool = False,
    ledger: bool = False,
) -> ModelMergeRecord:
    return ModelMergeRecord(
        repo=repo,
        number=number,
        committed_at=when,
        author=author,
        subject=f"{subject} (#{number})",
        docs_only=docs,
        ledger_only=ledger,
    )


def merges() -> tuple[ModelMergeRecord, ...]:
    """Product, a fix-forward, a revert, docs, a bot, a ledger roll, and an umbrella ticket's fixes."""
    records = [
        _merge("omnimarket", 1, at(28, 10), "feat(OMN-100): a feature"),
        _merge("omnimarket", 2, at(28, 12), "fix(OMN-100): its defect"),
        _merge("omnimarket", 3, at(29, 10), "feat(OMN-200): another feature"),
        _merge(
            "omnimarket", 4, at(29, 11), "revert(OMN-200): back out another feature"
        ),
        _merge("omnimarket", 5, at(29, 12), "docs: words", docs=True),
        _merge("omnimarket", 6, at(29, 13), "chore: bump", author="app[bot]"),
        _merge(
            "omnibase_core",
            7,
            at(29, 14),
            "docs(OMN-1): ledger rows through 2026-10-29T14:00:00Z",
            ledger=True,
        ),
    ]
    records += [
        _merge("omnimarket", 100 + i, at(28, 17 + i), "fix(OMN-999): umbrella fix")
        for i in range(5)
    ]
    return tuple(records)


def request(**overrides: object) -> ModelProcessHealthRequest:
    """The base request; overrides replace whole fields."""
    horizon = {"covered_from": SOURCES_FROM, "covered_to": GENERATED_AT}

    def read(
        name: EnumSourceName,
        covered_from: datetime = SOURCES_FROM,
        gaps: tuple[ModelTimeSpan, ...] = (),
    ) -> ModelSourceRead:
        return ModelSourceRead(
            name=name,
            status=EnumReadStatus.READ,
            locator="fixture",
            covered_from=covered_from,
            covered_to=horizon["covered_to"],
            gaps=gaps,
        )

    sources = (
        read(EnumSourceName.LEDGER, covered_from=LEDGER_FROM),
        read(
            EnumSourceName.REPO_STATUS_EVENTS,
            gaps=(ModelTimeSpan(start=at(28, 14), end=at(28, 20)),),
        ),
        read(EnumSourceName.GIT_MERGES),
        read(EnumSourceName.LANDING_DECISIONS),
        read(EnumSourceName.LANDING_COST),
        read(EnumSourceName.LANDING_DELEGATION),
        read(EnumSourceName.LANDING_METRICS_HISTORY),
        read(EnumSourceName.LINEAR_SNAPSHOT),
    )
    base: dict[str, object] = {
        "generated_at": GENERATED_AT,
        "config": contract_config(),
        "sources": sources,
        "ledger_rows": ledger_rows(),
        "repo_status_events": (
            ModelRepoStatusEvent(
                at=at(28, 2), repo="omnimarket", verdict="base_branch_red", check="C1"
            ),
            ModelRepoStatusEvent(
                at=at(28, 5), repo="omnimarket", verdict="merging_normally"
            ),
            ModelRepoStatusEvent(
                at=at(29, 22), repo="omnimarket", verdict="base_branch_red", check="C2"
            ),
        ),
        "merges": merges(),
        "decisions": (
            ModelControllerDecision(
                ts=at(28, 9),
                pr="omnimarket#1",
                head="",
                kind="update_branch",
                outcome="skipped",
            ),
            ModelControllerDecision(
                ts=at(28, 9, 30),
                pr="omnimarket#1",
                head="h1",
                kind="dispatch_worker",
                outcome="spawned",
            ),
            ModelControllerDecision(
                ts=at(28, 9, 40),
                pr="omnimarket#1",
                head="h1",
                kind="dispatch_worker",
                outcome="spawned",
            ),
            ModelControllerDecision(
                ts=at(29, 9, 30),
                pr="omnimarket#3",
                head="",
                kind="update_branch",
                outcome="skipped",
            ),
            ModelControllerDecision(
                ts=at(29, 9, 45),
                pr="omnimarket#2",
                head="h2",
                kind="dispatch_worker",
                outcome="spawned",
            ),
        ),
        "leases": (
            ModelControllerLease(
                kind="cause",
                subject="cause:x",
                repo="omnimarket",
                spawned_at=at(28, 2),
                released_at=at(28, 2, 10),
                worker_minutes=10.0,
            ),
            ModelControllerLease(
                kind="cause",
                subject="cause:x",
                repo="omnimarket",
                spawned_at=at(29, 3),
                released_at=at(29, 3, 20),
                worker_minutes=20.0,
            ),
            ModelControllerLease(
                kind="pr",
                subject="OmniNode-ai/omnimarket#1",
                repo="omnimarket",
                spawned_at=at(28, 9),
                released_at=at(28, 9, 6),
                worker_minutes=6.0,
            ),
        ),
        "delegation_rows": (
            ModelDelegationRow(ts=at(28, 9), lease_id=1, delegated=1),
            ModelDelegationRow(ts=at(28, 9, 5), lease_id=2, delegated=0),
            ModelDelegationRow(ts=at(29, 9), lease_id=3, delegated=2),
        ),
        "landing_metrics": (
            ModelLandingMetricsWindow(
                window_end=at(28, 12),
                green_to_merged_n=10,
                median_green_to_merged_min=20.0,
            ),
            ModelLandingMetricsWindow(
                window_end=at(28, 18),
                green_to_merged_n=30,
                median_green_to_merged_min=60.0,
            ),
        ),
        "linear_issues": (
            ModelLinearIssue(identifier="OMN-1", created_at=at(28, 3), labels=("bug",)),
            ModelLinearIssue(
                identifier="OMN-2",
                created_at=at(29, 3),
                completed_at=at(29, 20),
                labels=("bug",),
            ),
            ModelLinearIssue(
                identifier="OMN-3", created_at=at(29, 4), labels=("feature",)
            ),
        ),
    }
    base.update(overrides)
    return ModelProcessHealthRequest.model_validate(base)
