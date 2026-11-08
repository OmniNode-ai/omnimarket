# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two pure rules of the harness reading. No clock read, no I/O.

``reading_from_result`` turns one run into a reading. ``reading_at`` applies the
staleness rule to a stored reading at a given time, so a harness that stopped running
reads NOT WORKING, and one that never ran reads UNKNOWN (which also blocks).
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.lab_test_harness.model_lab_test_harness import (
    EnumLabTestHarnessReadingState,
    ModelLabTestHarnessReading,
    ModelLabTestHarnessResult,
)

_STATE = EnumLabTestHarnessReadingState


def reading_from_result(
    result: ModelLabTestHarnessResult, *, max_age_seconds: int, now: datetime
) -> ModelLabTestHarnessReading:
    """WORKING only when every case ended as its kind requires."""
    wrong = [o for o in result.outcomes if not o.as_expected]
    if wrong:
        parts = [
            f"{o.case_id} ({o.kind.value}) ended {o.verdict.value}"
            + (" (a broken input did not go red)" if o.verdict.value == "pass" else "")
            for o in wrong
        ]
        state, reason = _STATE.NOT_WORKING, "; ".join(parts)
    else:
        state = _STATE.WORKING
        reason = f"{len(result.outcomes)} cases ended as required"
    return ModelLabTestHarnessReading(
        piece=result.piece,
        tag=result.tag,
        state=state,
        blocks_admission=state is not _STATE.WORKING,
        reason=reason,
        run_id=result.run_id,
        overlay_revision=result.overlay_revision,
        probed_at=result.finished_at,
        published_at=now,
        max_age_seconds=max_age_seconds,
        outcomes=result.outcomes,
    )


def reading_at(
    stored: ModelLabTestHarnessReading | None,
    *,
    piece: str,
    tag: str,
    max_age_seconds: int,
    now: datetime,
) -> ModelLabTestHarnessReading:
    """The reading as it stands at ``now``: stale or future-dated reads NOT WORKING."""
    if stored is None:
        return ModelLabTestHarnessReading(
            piece=piece,
            tag=tag,
            state=_STATE.UNKNOWN,
            blocks_admission=True,
            reason="no result has been published",
            published_at=now,
            max_age_seconds=max_age_seconds,
        )
    if stored.piece != piece or stored.tag != tag:
        return stored.model_copy(
            update={
                "state": _STATE.NOT_WORKING,
                "blocks_admission": True,
                "reason": f"stored row is {stored.piece}/{stored.tag}, not {piece}/{tag}",
                "published_at": now,
            }
        )
    if stored.probed_at is None:
        return stored.model_copy(
            update={
                "state": _STATE.UNKNOWN,
                "blocks_admission": True,
                "reason": "the stored row has no probed_at",
                "published_at": now,
            }
        )
    age = (now - stored.probed_at).total_seconds()
    if age < 0:
        return stored.model_copy(
            update={
                "state": _STATE.NOT_WORKING,
                "blocks_admission": True,
                "reason": f"probed_at is {-age:.0f}s after the read time",
                "published_at": now,
                "max_age_seconds": max_age_seconds,
            }
        )
    if age > max_age_seconds:
        return stored.model_copy(
            update={
                "state": _STATE.NOT_WORKING,
                "blocks_admission": True,
                "reason": f"stale: probed {age:.0f}s ago, max age {max_age_seconds}s",
                "published_at": now,
                "max_age_seconds": max_age_seconds,
            }
        )
    return stored.model_copy(
        update={"published_at": now, "max_age_seconds": max_age_seconds}
    )
