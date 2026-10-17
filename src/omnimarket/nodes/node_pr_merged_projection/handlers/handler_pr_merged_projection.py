# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrMergedProjection — project pr-merged events to the projection store.

Consumes ``onex.evt.github.pr-merged.v1`` (published by the pr-merged-publisher
GHA workflow on every repo merge, OMN-13226 / T2) and UPSERTs one row per event
into the ``pr_merged_events`` table, deduped by ``event_id``. Idempotent:
projecting the same event twice leaves exactly one row.

The generic projection API serves the materialized rows at
``GET /projection/onex.evt.github.pr-merged.v1?since=<cursor>`` on the local
``:3002`` lane. The per-machine worktree reaper (OMN-13228 / T4) polls that
endpoint, matches ``{repo, branch, pr_number, ticket}`` to a local worktree, and
runs ``prune-worktrees.sh`` against it.

It also consumes ``onex.evt.omnimarket.repo-merged.v1`` (OMN-19998): the PR
watcher publishes one per merge it observes, within one tick, for every watched
repository, with no Actions workflow and no webhook. That event names no head
branch and no ticket, so its row carries the git-mirror fallback branch
``refs/pull/<n>/head`` and an empty ticket, and its ``event_id`` is the shared
UUIDv5 of ``(repo, pr_number, merge sha)`` that the git mirror and the webhook
fold use, so one merge seen by several of them stays one row. A repo-merged event
whose ``state`` is not ``merged`` writes nothing and
reports why in ``skipped_reason``.

The reducer performs no broker, no URL, and no filesystem I/O — it folds one
event into one projection row through the injected database adapter. It builds
no SQL: the row is a dict of typed fields handed to ``DatabaseAdapter.upsert``,
which binds them as parameters, and the ``refs/pull/<n>/head`` branch is composed
from the validated integer ``pr_number`` alone.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.github import ModelPrMergedEvent, pr_merged_event_id
from omnimarket.events.topics import REPO_MERGED_TOPIC_V1
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.projection.handler_shim import (
    INJECTED_TOPIC_KEY,
    split_projection_input,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "pr_merged_events"
CONFLICT_KEY = "event_id"


class ModelProjectionResult(BaseModel):
    """Outcome of projecting one pr-merged event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)
    skipped_reason: str | None = Field(
        default=None,
        description="Why the event wrote no row, when it was read and deliberately not folded.",
    )


class HandlerPrMergedProjection:
    """Project ``onex.evt.github.pr-merged.v1`` events into ``pr_merged_events``."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """Runtime dispatch shim.

        The projection host delivers the event payload plus a ``DatabaseAdapter``
        under ``input_data['_db']`` and the source topic under ``_topic``. A
        repo-merged event is folded into the pr-merged shape first; anything
        else is read as a ``ModelPrMergedEvent``. Fail fast when the adapter is
        absent — a missing adapter is a wiring bug, not a recoverable state.
        """
        db, payload, meta = split_projection_input(input_data)
        if meta.get(INJECTED_TOPIC_KEY) == REPO_MERGED_TOPIC_V1:
            merged = ModelRepoMerged.model_validate(payload)
            if merged.state != "merged":
                return ModelProjectionResult(
                    skipped_reason=(
                        f"{merged.repo}#{merged.pr_number} is {merged.state}, not merged"
                    )
                ).model_dump(mode="json")
            event = pr_merged_from_repo_merged(merged)
        else:
            event = ModelPrMergedEvent.model_validate(payload)
        result = self.project(event, db)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelPrMergedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single pr-merged event, deduped by ``event_id``.

        ``projection_cursor`` is a DB-assigned monotonic BIGSERIAL and is never
        written by the handler, so the cursor advances strictly on insert.
        """
        row: dict[str, object] = {
            "event_id": event.event_id,
            "repo": event.repo,
            "branch": event.branch,
            "pr_number": event.pr_number,
            "ticket": event.ticket,
            "merged_at": event.merged_at,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)


def pr_merged_from_repo_merged(event: ModelRepoMerged) -> ModelPrMergedEvent:
    """The pr-merged row a repo-merged event stands for.

    ``event_id`` is the shared ``(repo, pr_number, merge sha)`` UUIDv5, the id
    the git mirror and the webhook fold give the same merge. The event carries
    no head branch or ticket: the branch is the git mirror's unresolved form.
    """
    return ModelPrMergedEvent(
        event_id=str(pr_merged_event_id(event.repo, event.pr_number, event.merge_sha)),
        repo=event.repo,
        branch=f"refs/pull/{event.pr_number}/head",
        pr_number=event.pr_number,
        ticket="",
        merged_at=event.merged_at.isoformat(),
    )


__all__: list[str] = [
    "HandlerPrMergedProjection",
    "ModelProjectionResult",
    "pr_merged_from_repo_merged",
]
