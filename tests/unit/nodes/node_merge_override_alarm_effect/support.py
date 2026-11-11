# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders, recording doubles and a driver for the merge override alarm tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_github_pr_state_fact import (
    ModelGithubPrStateFact,
)
from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaReading,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.github_landing.github_landing_requests import pull_request_request
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_merge_override_alarm_effect.handlers import (
    handler_merge_override_alarm,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm import (
    HandlerMergeOverrideAlarm,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelLedgerRow,
    ModelMergeOverrideResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.node_overlay_support import install_node_overlay

NODE = "node_merge_override_alarm_effect"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnimarket"
PR = 7
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
MERGE_COMMIT = "c" * 40
APP = "example-landing-app"
HAND = "example-person-login"
SETTINGS: dict[str, object] = {
    "landing_app_login": APP,
    "s19_repositories": [],
    "pending_read_timeout_seconds": 600,
    "sweep_interval_seconds": 60,
}
SLACK_SUFFIX = "slack-publish.v1"
FINDING_SUFFIX = "merge-override-finding.v1"
VERDICT_SUFFIX = "merge-override-verdict.v1"
READ_SUFFIX = "pr-landing-github-requested.v1"


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.value = at.timestamp()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Publisher:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error = error

    def __call__(self, topic: str, payload: bytes) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((topic, json.loads(payload)))

    def on(self, suffix: str) -> list[dict[str, object]]:
        return [body for topic, body in self.calls if topic.endswith(suffix)]


class Reader:
    def __init__(self, rows: list[ModelLedgerRow], error: str | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls = 0

    def read_override_rows(
        self, repository: str, pr_number: int
    ) -> list[ModelLedgerRow]:
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return list(self.rows)


class Rig:
    def __init__(
        self,
        handler: HandlerMergeOverrideAlarm,
        publisher: Publisher,
        database: InmemoryDatabaseAdapter,
        reader: Reader,
        clock: Clock,
    ) -> None:
        self.handler = handler
        self.publisher = publisher
        self.database = database
        self.reader = reader
        self.clock = clock

    def rows(self, table: str) -> list[dict[str, object]]:
        return self.database.query(table, None)


def make_rig(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    rows: list[ModelLedgerRow] | None = None,
    reader_error: str | None = None,
    settings: dict[str, object] | None = None,
    publisher: Publisher | None = None,
) -> Rig:
    install_node_overlay(monkeypatch, root, NODE, settings or SETTINGS)
    monkeypatch.setattr(
        handler_merge_override_alarm,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )
    publisher = publisher if publisher is not None else Publisher()
    database = InmemoryDatabaseAdapter()
    reader = Reader(rows or [], reader_error)
    clock = Clock()
    handler = HandlerMergeOverrideAlarm(
        event_publisher=publisher, database=database, reader=reader, clock=clock
    )
    return Rig(handler, publisher, database, reader, clock)


def ruling_row(
    cells: str, *, age_seconds: int = 3600, row_type: str = "RULING"
) -> ModelLedgerRow:
    row_ts = NOW - timedelta(seconds=age_seconds)
    raw = f"{row_ts:%Y-%m-%dT%H:%M:%SZ} | {row_type} | lane=orchestrator-test | {cells} | text"
    return ModelLedgerRow(
        row_id=hashlib.sha256(raw.encode()).hexdigest(),
        row_ts=row_ts,
        row_type=row_type,
        row_lane="orchestrator-test",
        raw_row=raw,
    )


def override_row(head: str = HEAD, pr: str = f"{REPO}#{PR}") -> ModelLedgerRow:
    return ruling_row(f"override=merge | pr={pr} | head={head}")


def pr_merged_event(pr: int = PR, repo: str = REPO) -> ModelPrMergedEvent:
    return ModelPrMergedEvent(
        event_id=str(uuid4()),
        repo=repo,
        branch="omn-1-example",
        pr_number=pr,
        merged_at=NOW.isoformat(),
    )


def repo_merged_event(pr: int = PR, repo: str = REPO) -> ModelRepoMerged:
    return ModelRepoMerged(
        repo=repo,
        base="dev",
        merge_sha=MERGE_COMMIT,
        pr_number=pr,
        merged_at=NOW,
        source="pr-watcher",
    )


def pr_state(
    *,
    pr: int = PR,
    head: str = HEAD,
    merged: bool = True,
    merger: str | None = HAND,
) -> ModelGithubPrStateFact:
    return ModelGithubPrStateFact(
        pr_node_id="PR_example",
        pr_number=pr,
        head_sha=head,
        base_ref="dev",
        draft=False,
        title="example change",
        labels=(),
        state="closed" if merged else "open",
        merged=merged,
        auto_merge_armed=False,
        merged_by=merger if merged else None,
        merge_commit_sha=MERGE_COMMIT if merged else None,
    )


def _quota() -> ModelGithubQuotaReading:
    return ModelGithubQuotaReading(
        limit=5000,
        remaining=4000,
        used=1000,
        reset=1794400000,
        resource="core",
        identity="GITHUB_TOKEN",
    )


def completed(
    operation: EnumPrLandingGithubOperation,
    *,
    mode: EnumPrLandingGithubMode = EnumPrLandingGithubMode.ENFORCE,
    head: str | None = HEAD,
    pr: int = PR,
    correlation_id: UUID | None = None,
    state: ModelGithubPrStateFact | None = None,
    not_modified: bool = False,
) -> ModelPrLandingGithubCompleted:
    enforce = mode is EnumPrLandingGithubMode.ENFORCE
    return ModelPrLandingGithubCompleted(
        correlation_id=correlation_id or uuid4(),
        operation=operation,
        mode=mode,
        repository=REPO,
        pr_number=pr,
        head_sha=head,
        requests=(pull_request_request(REPO, pr),),
        http_statuses=(200,) if enforce else (),
        not_modified=not_modified,
        pr_state=state,
        quota=_quota() if enforce else None,
    )


def arm_receipt(
    head: str = HEAD,
    *,
    operation: EnumPrLandingGithubOperation = EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
    mode: EnumPrLandingGithubMode = EnumPrLandingGithubMode.ENFORCE,
    pr: int = PR,
) -> ModelPrLandingGithubCompleted:
    return completed(
        operation,
        mode=mode,
        head=head,
        pr=pr,
        state=pr_state(pr=pr, head=head, merged=False)
        if mode is EnumPrLandingGithubMode.ENFORCE
        else None,
    )


def read_answer(
    rig: Rig,
    *,
    state: ModelGithubPrStateFact | None = None,
    pr: int = PR,
    not_modified: bool = False,
) -> ModelPrLandingGithubCompleted:
    """The landing effect's answer to the node's last published read_pr_state command."""
    commands = rig.publisher.on(READ_SUFFIX)
    assert commands, "the node published no read_pr_state command"
    return completed(
        EnumPrLandingGithubOperation.READ_PR_STATE,
        head=None,
        pr=pr,
        correlation_id=UUID(str(commands[-1]["correlation_id"])),
        state=None
        if not_modified
        else (state if state is not None else pr_state(pr=pr)),
        not_modified=not_modified,
    )


def drive_merge(
    rig: Rig,
    *,
    state: ModelGithubPrStateFact | None = None,
    pr: int = PR,
) -> ModelMergeOverrideResult:
    """Send the merge event, answer its read, and return the verdict result."""
    requested = rig.handler.handle(pr_merged_event(pr))
    assert requested.outcome.value == "read_requested", requested
    return rig.handler.handle(read_answer(rig, state=state, pr=pr))
