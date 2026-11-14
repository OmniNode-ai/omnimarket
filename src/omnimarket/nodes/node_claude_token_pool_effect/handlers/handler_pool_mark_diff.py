# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure diff behind the pool's mark events: what one operation changed (OMN-19998).

The caller hands the pool's entries before and after the operation, already reduced to live marks —
an ``until`` that is only in the past is not in them — and :func:`mark_events` compares the entries
per label: a limit stamp newly set, replaced while set, or cleared; a revocation stamp newly set
(a challenge when the after entry's ``revoke_cause`` says so) or cleared. The comparison is plain
presence and value: no instant is parsed here. A label whose rows are absent from one side reads as
an entry without marks, and a challenge upgraded to a real revoke while its stamp stays names
``revoke_confirmed``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
    ModelPoolMarkDiffRequest,
    ModelPoolMarkDiffResult,
    ModelPoolMarkEvent,
    PoolMarkChange,
)


def _limit_change(was: Any, now: Any) -> PoolMarkChange | None:
    """What happened to the limit stamp, ``None`` when it did not change."""
    if was is None:
        return "limited" if now is not None else None
    if now is None:
        return "limit_cleared"
    return "limit_replaced" if now != was else None


def _revoke_change(
    was: Any, now: Any, was_cause: Any, cause: Any
) -> PoolMarkChange | None:
    """What happened to the revocation stamp (or to its cause while it stays), ``None`` when nothing did."""
    if was is None:
        if now is None:
            return None
        return "challenged" if cause == "challenged" else "revoked"
    if now is None:
        return "revoke_cleared"
    if was_cause == "challenged" and cause == "revoked":
        return "revoke_confirmed"
    return None


def mark_events(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    *,
    who: str,
    at: datetime,
    verb: str,
    host: str,
    cause_for: Callable[[str, str], str],
) -> tuple[ModelPoolMarkEvent, ...]:
    """The events one pool operation leaves behind: one per changed mark, none when nothing changed.

    Labels are taken in sorted order, and per label a limit change is named before a revoke change,
    so one pass that both replaces a limit and clears a revoke leaves two events in that order. The
    model carries the vocabulary: an unknown ``verb`` (``verb`` is typed ``str`` here) or a blank
    ``who`` is refused with a validation error before any event exists.
    """
    changes: list[tuple[str, PoolMarkChange]] = []
    for label in sorted(set(before) | set(after)):
        was = before.get(label) or {}
        now = after.get(label) or {}
        limit = _limit_change(was.get("until"), now.get("until"))
        if limit is not None:
            changes.append((label, limit))
        revoke = _revoke_change(
            was.get("revoked_at"),
            now.get("revoked_at"),
            was.get("revoke_cause"),
            now.get("revoke_cause"),
        )
        if revoke is not None:
            changes.append((label, revoke))
    return tuple(
        ModelPoolMarkEvent(
            who=who,
            at=at,
            verb=verb,
            host=host,
            label=label,
            change=change,
            cause=cause_for(verb, change),
        )
        for label, change in changes
    )


class HandlerPoolMarkDiff:
    """The contract's ``mark_diff`` handler: one operation's marks before and after in, the events it left out."""

    def handle(self, request: ModelPoolMarkDiffRequest) -> ModelPoolMarkDiffResult:
        return ModelPoolMarkDiffResult(
            events=mark_events(
                request.before,
                request.after,
                who=request.who,
                at=request.at,
                verb=request.verb,
                host=request.host,
                cause_for=lambda _verb, _change: request.cause,
            )
        )


__all__ = ["HandlerPoolMarkDiff", "mark_events"]
