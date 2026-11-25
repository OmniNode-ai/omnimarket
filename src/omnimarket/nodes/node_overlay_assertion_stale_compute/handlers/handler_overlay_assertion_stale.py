# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""COMPUTE: which listed overlays have a loader assertion older than the bound, and who acts on each.

An assertion is the loader's own record of one overlay (id, digest, time), kept apart from what a set writes, so its age
measures the loader and nothing a person did. An id alarms by name when its newest assertion is older than the bound, or
when it has none; the alarm carries the owner (a role or lane, never a person) so the party who acts on it is in the
payload. This alarm serves until the heartbeat replaces it.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.overlay_store import (
    ModelOverlayAssertionCheck,
    ModelOverlayAssertionCheckResult,
    ModelOverlayAssertionStaleAlarm,
)


class HandlerOverlayAssertionStale:
    """One alarm per listed id whose newest assertion is past the bound."""

    def handle(
        self, request: ModelOverlayAssertionCheck
    ) -> ModelOverlayAssertionCheckResult:
        newest: dict[str, datetime] = {}
        for held in request.assertions:
            seen = newest.get(held.overlay_id)
            if seen is None or held.asserted_at > seen:
                newest[held.overlay_id] = held.asserted_at
        alarms: list[ModelOverlayAssertionStaleAlarm] = []
        fresh: list[str] = []
        for overlay_id in request.overlay_ids:
            owner = request.owner_by_id.get(overlay_id, request.owner)
            at = newest.get(overlay_id)
            if at is None:
                alarms.append(
                    ModelOverlayAssertionStaleAlarm(
                        overlay_id=overlay_id,
                        owner=owner,
                        newest_assertion_at=None,
                        age_s=None,
                        bound_s=request.stale_after_s,
                        reason=f"overlay {overlay_id}: no assertion by the loader is held",
                        raised_at=request.now,
                    )
                )
                continue
            age = (request.now - at).total_seconds()
            if age > request.stale_after_s:
                alarms.append(
                    ModelOverlayAssertionStaleAlarm(
                        overlay_id=overlay_id,
                        owner=owner,
                        newest_assertion_at=at,
                        age_s=age,
                        bound_s=request.stale_after_s,
                        reason=(
                            f"overlay {overlay_id}: the newest loader assertion is {age:.0f}s old, "
                            f"past the bound of {request.stale_after_s}s"
                        ),
                        raised_at=request.now,
                    )
                )
            else:
                fresh.append(overlay_id)
        return ModelOverlayAssertionCheckResult(
            alarms=tuple(alarms), fresh=tuple(fresh)
        )


__all__ = ["HandlerOverlayAssertionStale"]
