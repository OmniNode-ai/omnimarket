# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invented policies, histories and recording ports shared by the gate tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.change_source.model_change_history import (
    EnumAccountType,
    EnumChangeEventKind,
    ModelChangeEvent,
    ModelChangeHistory,
)
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumMergeMethod,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseEvent,
    ModelReleaseActor,
    ModelReleasePullRequestFacts,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelCheckReleaseGateRequest,
    ModelReadReleaseGrantRequest,
    ModelRecordAuthorizationReceiptRequest,
)
from omnimarket.protocols.protocol_change_source import ProtocolChangeSource
from omnimarket.protocols.protocol_clock import ProtocolClock
from omnimarket.protocols.protocol_work_ledger_store import ProtocolWorkLedgerStore

REPOSITORY = "fixture-repo"
PULL_REQUEST = 7
HEAD = "a" * 40
NEW_HEAD = "b" * 40
PUSH_AT = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
LABEL_AT = PUSH_AT + timedelta(minutes=5)
LATER = LABEL_AT + timedelta(minutes=5)
NOW = LATER + timedelta(minutes=5)
FIXTURE_LOGINS = (
    "fixture-operator",
    "fixture-cloud-owner",
    "fixture-agent",
    "fixture-train",
    "fixture-tagger",
    "fixture-outsider",
)


def policy(**overrides: Any) -> ModelReleaseGatePolicy:
    values: dict[str, Any] = {
        "grant_label": "fixture-release-grant",
        "grantor_roles": ("operator", "cloud_owner"),
        "role_logins": {
            "operator": ("fixture-operator",),
            "cloud_owner": ("fixture-cloud-owner",),
        },
        "agent_identities": ("fixture-agent",),
        "train": {
            "logins": ("fixture-train",),
            "run_identities": ("fixture-train-run",),
            "branch_prefixes": ("fixture-release/",),
        },
        "tagger": {
            "logins": ("fixture-tagger",),
            "run_identities": ("fixture-tagger-run",),
            "tag_patterns": (r"^dev-merge-\d+$",),
        },
        "shapes": {
            "pr_title_prefixes": ("release:",),
            "pr_labels": ("release",),
            "pr_branch_prefixes": ("fixture-release/",),
            "version_files": ("pyproject.toml",),
            "version_tag_patterns": (r"^v\d+\.\d+\.\d+",),
        },
    }
    values.update(overrides)
    return ModelReleaseGatePolicy(**values)


def labeled(
    id: str,
    at: datetime,
    login: str,
    type: EnumAccountType = EnumAccountType.USER,
    via_app: bool = False,
) -> ModelChangeEvent:
    return ModelChangeEvent(
        event_id=id,
        kind=EnumChangeEventKind.LABELED,
        at=at,
        actor_login=login,
        actor_type=type,
        via_app=via_app,
        label="fixture-release-grant",
    )


def unlabeled(
    id: str,
    at: datetime,
    login: str,
    type: EnumAccountType = EnumAccountType.USER,
    via_app: bool = False,
) -> ModelChangeEvent:
    return labeled(id, at, login, type, via_app).model_copy(
        update={"kind": EnumChangeEventKind.UNLABELED}
    )


def pushed(id: str, at: datetime, head_sha: str, login: str) -> ModelChangeEvent:
    return ModelChangeEvent(
        event_id=id,
        kind=EnumChangeEventKind.PUSHED,
        at=at,
        actor_login=login,
        actor_type=EnumAccountType.USER,
        head_sha=head_sha,
    )


def standing_grant() -> tuple[ModelChangeEvent, ...]:
    return (
        pushed("push-1", PUSH_AT, HEAD, "fixture-agent"),
        labeled("label-1", LABEL_AT, "fixture-operator"),
    )


def pushed_after_grant() -> tuple[ModelChangeEvent, ...]:
    return (*standing_grant(), pushed("push-2", LATER, NEW_HEAD, "fixture-agent"))


def ledger_rows(events: tuple[ModelChangeEvent, ...]) -> list[dict[str, str]]:
    return [
        {
            "kind": event.kind.value,
            "event_id": event.event_id,
            "at": event.at.isoformat(),
            "actor": event.actor_login,
            "actor_type": event.actor_type.value,
            "via_app": str(event.via_app).lower(),
            "label": event.label or "",
            "head_sha": event.head_sha or "",
            "repository": REPOSITORY,
            "pull_request": str(PULL_REQUEST),
        }
        for event in events
    ]


def timeline_payload(events: tuple[ModelChangeEvent, ...]) -> list[dict[str, object]]:
    return [
        {
            "id": event.event_id,
            "event": "head_ref_force_pushed"
            if event.kind is EnumChangeEventKind.PUSHED
            else event.kind.value,
            "created_at": event.at.isoformat(),
            "actor": {
                "login": event.actor_login,
                "type": event.actor_type.value.title(),
            },
            "performed_via_github_app": {"slug": "fixture-app"}
            if event.via_app
            else None,
            "label": {"name": event.label},
            "sha": event.head_sha,
        }
        for event in events
    ]


class FixedClock(ProtocolClock):
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.at


class RecordingLedger(ProtocolWorkLedgerStore):
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[tuple[str, dict[str, str]]] = []
        self.fail = fail
        self.attempts = 0

    def append_row(self, kind: str, fields: Mapping[str, str]) -> None:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("fixture ledger unavailable")
        self.rows.append((kind, dict(fields)))


class HistorySource(ProtocolChangeSource):
    def __init__(
        self,
        events: tuple[ModelChangeEvent, ...] = (),
        *,
        histories: dict[tuple[str, int], tuple[ModelChangeEvent, ...]] | None = None,
        raise_on_read: bool = False,
        bodies: tuple[str, ...] = (),
    ) -> None:
        self.histories = (
            {(REPOSITORY, PULL_REQUEST): events} if histories is None else histories
        )
        self.raise_on_read = raise_on_read
        self.bodies = bodies
        self.reads = 0

    def change_history(self, repository: str, pull_request: int) -> ModelChangeHistory:
        self.reads += 1
        if self.raise_on_read:
            raise RuntimeError("fixture history unavailable")
        return ModelChangeHistory(
            repository=repository,
            pull_request=pull_request,
            events=self.histories.get((repository, pull_request), ()),
        )


class PresentStateSource(HistorySource):
    """Deliberately inadequate source exposing only a currently applied label."""

    def __init__(self) -> None:
        super().__init__((labeled("label-1", LABEL_AT, "fixture-operator"),))


def read_request(**overrides: Any) -> ModelReadReleaseGrantRequest:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "pull_request": PULL_REQUEST,
        "head_sha": HEAD,
        "boundary": EnumGateBoundary.CHECK,
        "policy": policy(),
    }
    values.update(overrides)
    return ModelReadReleaseGrantRequest(**values)


def receipt_request(**overrides: Any) -> ModelRecordAuthorizationReceiptRequest:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "pull_request": PULL_REQUEST,
        "head_sha": HEAD,
        "merge_commit_sha": "c" * 40,
        "merge_method": EnumMergeMethod.SQUASH,
        "policy": policy(),
    }
    values.update(overrides)
    return ModelRecordAuthorizationReceiptRequest(**values)


def check_request(**overrides: Any) -> ModelCheckReleaseGateRequest:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "event": EnumReleaseEvent.PULL_REQUEST,
        "actor": ModelReleaseActor(
            login="fixture-agent",
            account_type=EnumAccountType.USER,
            run_identity="agent-run",
        ),
        "boundary": EnumGateBoundary.CHECK,
        "pull_request": ModelReleasePullRequestFacts(
            number=PULL_REQUEST,
            title="release: v1.2.3",
            head_branch="feature/x",
            head_sha=HEAD,
        ),
        "changed_files": ("pyproject.toml",),
    }
    values.update(overrides)
    return ModelCheckReleaseGateRequest(**values)
