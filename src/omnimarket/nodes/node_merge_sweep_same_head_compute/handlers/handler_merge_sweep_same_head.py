# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure watcher evidence and the merge-sweep same-head skip (OMN-20864)."""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import Any

from omnimarket.nodes.node_merge_sweep_same_head_compute.models.model_merge_sweep_same_head import (
    ModelMergeSweepSameHeadRequest,
    ModelMergeSweepSameHeadResult,
)


def queue_entry(rec: Mapping[str, Any]) -> dict[str, Any] | None:
    """The watcher's merge-queue entry and reason, only while queued (OMN-20864)."""
    if not rec.get("queued"):
        return None
    queue = rec.get("queue") or {}
    return {
        "state": queue.get("state"),
        "position": queue.get("position"),
        "enqueued_at": queue.get("enqueued_at"),
        "reason": rec.get("reason"),
    }


def watcher_handoff(rec: Mapping[str, Any], head: str | None) -> dict[str, Any] | None:
    """The watcher's handoff at the current head, including pr-less MSGs (OMN-20864)."""
    handoff = rec.get("handoff") or {}
    want = str(head or "").strip().lower()
    if want and str(handoff.get("head") or "").strip().lower() == want:
        return {
            "ts": handoff.get("at"),
            "from": handoff.get("lane"),
            "head": handoff.get("head"),
        }
    return None


def same_head_skip(
    h: Mapping[str, Any], key: str, scope: Set[str]
) -> dict[str, Any] | None:
    """The skipped entry for an open, in-scope chain head already queued or handed off (OMN-20864).

    A chain head in the merge queue or handed off at its current head (ledger or watcher) plans no lane
    (``queued-or-handed-off-same-head``); a handoff behind a strict order keeps ``handed-off-same-head``.
    """
    # OMN-20864: queued or handed off at this head: a lane would re-hand the same head every sweep.
    if (
        (h.get("handed_off") or h.get("queued"))
        and key.split("#", 1)[0] in scope
        and str(h.get("state", "UNKNOWN")).upper() == "OPEN"
    ):
        handed, queued = h.get("handed_off"), h.get("queued") or {}
        head = handed.get("head") if handed else h.get("head")
        reason = (
            h.get("strict_queued")
            or queued.get("reason")
            or (
                f"handed off to landing-controller at {str(head or '')[:12]} at {handed.get('ts')}, no head change since"
                if handed
                else None
            )
        )
        return {
            "pr": key,
            "why": "handed-off-same-head"
            if handed and h.get("strict_queued")
            else "queued-or-handed-off-same-head",
            "head": head,
            "reason": reason,
        }
    return None


class HandlerMergeSweepSameHead:
    def handle(
        self, request: ModelMergeSweepSameHeadRequest
    ) -> ModelMergeSweepSameHeadResult:
        return ModelMergeSweepSameHeadResult(
            skipped=same_head_skip(request.head, request.key, request.scope)
        )
