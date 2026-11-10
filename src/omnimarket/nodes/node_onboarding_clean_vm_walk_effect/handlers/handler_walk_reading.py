# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly clean-VM walk reading: a pure function of the walk receipt and a clock.

``handle(ModelWalkReadingRequest) -> ModelWalkReading``. It reads no clock, file, network
or environment; the caller supplies ``now`` and the overlay's max age. The rules (plan
section 3.5 row 18 and section 4.2 row 18):

* A walk that does not end in a delegated result reads NOT_WORKING.
* A walk older than the max age (strictly) reads NOT_WORKING: stale is a failure.
* No walk at all reads UNKNOWN, which blocks exactly as NOT_WORKING does.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkOutcome,
    ModelWalkReceipt,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_walk_reading import (
    ModelWalkReading,
    ModelWalkReadingOverlay,
    ModelWalkReadingRequest,
)

#: The overlay id of this node's reading settings: <root>/<id>/overlay.yaml.
WALK_READING_OVERLAY_ID = "node_onboarding_clean_vm_walk_effect"

REASON_NO_WALK = "no-walk"
REASON_NOT_DELEGATED = "walk-not-delegated"
REASON_STALE = "stale-walk"
REASON_FINISHED_AFTER_NOW = "walk-finished-after-now"
REASON_TIME_UNREADABLE = "walk-time-unreadable"


def load_walk_reading_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelWalkReadingOverlay:
    """The reading's settings from the first overlay root; none supplied is refused."""
    return load_structured_overlay(
        WALK_READING_OVERLAY_ID, ModelWalkReadingOverlay, environ
    )


def walk_ends_in_delegated_result(receipt: ModelWalkReceipt) -> bool:
    """True only when the walk passed and its log holds a successful delegated run."""
    return (
        receipt.outcome == EnumWalkOutcome.PASSED
        and receipt.delegated_run.status == "success"
        and bool(receipt.delegated_run.run_id)
    )


def _published_at(receipt: ModelWalkReceipt) -> datetime | None:
    try:
        moment = datetime.fromisoformat(receipt.finished_at)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


class HandlerWalkReading:
    """Pure compute: the verdict of the weekly walk."""

    def handle(self, request: ModelWalkReadingRequest) -> ModelWalkReading:
        receipt = request.receipt
        if receipt is None:
            return ModelWalkReading(
                verdict=EnumReadingVerdict.UNKNOWN,
                reasons=(REASON_NO_WALK,),
                ends_in_delegated_result=False,
                probed_at=request.now,
                published_at=None,
                age_seconds=None,
                max_age_seconds=request.max_age_seconds,
            )
        reasons: list[str] = []
        delegated = walk_ends_in_delegated_result(receipt)
        if not delegated:
            reasons.append(REASON_NOT_DELEGATED)
        published = _published_at(receipt)
        age: int | None = None
        if published is None:
            reasons.append(REASON_TIME_UNREADABLE)
        else:
            elapsed = (request.now - published).total_seconds()
            age = int(elapsed)
            if elapsed < 0:
                reasons.append(REASON_FINISHED_AFTER_NOW)
            elif elapsed > request.max_age_seconds:
                reasons.append(REASON_STALE)
        return ModelWalkReading(
            verdict=(
                EnumReadingVerdict.NOT_WORKING
                if reasons
                else EnumReadingVerdict.WORKING
            ),
            reasons=tuple(reasons),
            ends_in_delegated_result=delegated,
            probed_at=request.now,
            published_at=published,
            age_seconds=age,
            max_age_seconds=request.max_age_seconds,
        )
