# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure conversions of recorded timeline events and ledger rows into history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from omnimarket.models.change_source.model_change_history import (
    EnumAccountType,
    EnumChangeEventKind,
    ModelChangeEvent,
    ModelChangeHistory,
)


def events_from_timeline(
    payload: Sequence[Mapping[str, object]], *, repository: str, pull_request: int
) -> ModelChangeHistory:
    """Preserve relevant timeline events in source order; reject malformed ones."""
    kinds = {
        "labeled": EnumChangeEventKind.LABELED,
        "unlabeled": EnumChangeEventKind.UNLABELED,
        "head_ref_force_pushed": EnumChangeEventKind.PUSHED,
        "pushed": EnumChangeEventKind.PUSHED,
    }
    account_types = {
        "User": EnumAccountType.USER,
        "Bot": EnumAccountType.BOT,
        "Organization": EnumAccountType.ORGANIZATION,
        "Mannequin": EnumAccountType.MANNEQUIN,
    }
    events: list[ModelChangeEvent] = []
    for item in payload:
        kind = kinds.get(str(item.get("event")))
        if kind is None:
            continue
        event_id, raw_actor = item.get("id"), item.get("actor")
        if (
            not isinstance(event_id, (int, str))
            or isinstance(event_id, bool)
            or not isinstance(raw_actor, Mapping)
        ):
            raise ValueError("timeline event requires an id and actor")
        actor = cast(Mapping[str, object], raw_actor)
        at = item.get("created_at", item.get("committer.date"))
        committer = item.get("committer")
        if at is None and isinstance(committer, Mapping):
            at = cast(Mapping[str, object], committer).get("date")
        if not isinstance(at, str):
            raise ValueError("timeline event requires an ISO timestamp")
        label = item.get("label")
        head_sha = next(
            (item[key] for key in ("after_sha", "sha", "commit_id") if key in item),
            None,
        )
        events.append(
            ModelChangeEvent.model_validate(
                {
                    "event_id": str(event_id),
                    "kind": kind,
                    "at": datetime.fromisoformat(at),
                    "actor_login": actor.get("login"),
                    "actor_type": account_types.get(
                        str(actor.get("type")), EnumAccountType.UNKNOWN
                    ),
                    "via_app": item.get("performed_via_github_app") is not None,
                    "label": cast(Mapping[str, object], label).get("name")
                    if kind is not EnumChangeEventKind.PUSHED
                    and isinstance(label, Mapping)
                    else None,
                    "head_sha": head_sha
                    if kind is EnumChangeEventKind.PUSHED
                    else None,
                }
            )
        )
    return ModelChangeHistory(
        repository=repository, pull_request=pull_request, events=tuple(events)
    )


def events_from_ledger_rows(
    rows: Sequence[Mapping[str, str]], *, repository: str, pull_request: int
) -> ModelChangeHistory:
    """Read matching change rows in input order without consulting other state."""
    events: list[ModelChangeEvent] = []
    for row in rows:
        if row.get("repository") != repository or row.get("pull_request") != str(
            pull_request
        ):
            continue
        if row.get("kind") not in {kind.value for kind in EnumChangeEventKind}:
            continue
        if row.get("via_app") not in {"true", "false"}:
            raise ValueError("ledger event requires a true or false app flag")
        kind = EnumChangeEventKind(row.get("kind", ""))
        events.append(
            ModelChangeEvent(
                event_id=row.get("event_id", ""),
                kind=kind,
                at=datetime.fromisoformat(row.get("at", "")),
                actor_login=row.get("actor", ""),
                actor_type=EnumAccountType(row.get("actor_type", "")),
                via_app=row.get("via_app") == "true",
                label=row.get("label")
                if kind is not EnumChangeEventKind.PUSHED
                else None,
                head_sha=row.get("head_sha")
                if kind is EnumChangeEventKind.PUSHED
                else None,
            )
        )
    return ModelChangeHistory(
        repository=repository, pull_request=pull_request, events=tuple(events)
    )
