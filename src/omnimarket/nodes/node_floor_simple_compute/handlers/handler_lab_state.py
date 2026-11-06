# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge every lab source by its reading: WORKING, NOT WORKING or UNKNOWN, with a maximum age (OMN-19998, 17a).

Pure: ``handle(ModelLabStateInput) -> ModelLabStateOutput``. The rule is the plan's reading rule (3.5): a source with
no read, an unread source or a failing read is not WORKING; a read older than the source's ``max_age_s`` is NOT
WORKING (a stale read counts as NOT WORKING); a read with a failing subject is NOT WORKING; a declared source the
reader returned nothing for is UNKNOWN, and is never dropped from the output. The age bound is the overlay's.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.floor_17a import (
    ModelLabSourceDef,
    ModelLabSourceRead,
    ModelLabSourceVerdict,
    ModelLabStateInput,
    ModelLabStateOutput,
)

FUTURE_TOLERANCE_S = 60


def _minutes(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


class HandlerLabState:
    """One verdict per declared source, in the overlay's order."""

    def handle(self, request: ModelLabStateInput) -> ModelLabStateOutput:
        reads = {r.id: r for r in request.reads}
        return ModelLabStateOutput.model_validate(
            {
                "computed_at": request.now,
                "verdicts": tuple(
                    self._verdict(d, reads.get(d.id), request.now)
                    for d in request.sources
                ),
            }
        )

    @staticmethod
    def _verdict(
        definition: ModelLabSourceDef, read: ModelLabSourceRead | None, now: datetime
    ) -> ModelLabSourceVerdict:
        state = "WORKING"
        reason = ""
        age: int | None = None
        if read is None:
            state, reason = "UNKNOWN", "nothing read this source"
        elif read.status == "unread":
            state, reason = "UNKNOWN", read.reason or "the source could not be read"
        elif read.status == "failed":
            state, reason = "NOT WORKING", read.reason or "the read failed"
        elif read.read_at is None:
            state, reason = "UNKNOWN", "the read carries no time"
        else:
            delta = int((now - read.read_at).total_seconds())
            age = max(delta, 0)
            if delta < -FUTURE_TOLERANCE_S:
                state, reason = "NOT WORKING", "the read is dated in the future"
            elif age > definition.max_age_s:
                state = "NOT WORKING"
                reason = (
                    f"stale: read {_minutes(age)} ago, the limit is "
                    f"{_minutes(definition.max_age_s)}"
                )
            else:
                bad = [s for s in read.subjects if not s.ok]
                if bad:
                    state = "NOT WORKING"
                    reason = "; ".join(f"{s.name}: {s.note}" for s in bad[:4])
                    if len(bad) > 4:
                        reason += f"; and {len(bad) - 4} more"
        probed = read.read_at if read is not None else None
        return ModelLabSourceVerdict.model_validate(
            {
                "id": definition.id,
                "title": definition.title,
                "kind": definition.kind,
                "state": state,
                "probed_at": probed,
                "published_at": now,
                "age_s": age,
                "max_age_s": definition.max_age_s,
                "reason": reason,
                "summary": read.summary if read is not None else "",
                "subjects": read.subjects if read is not None else (),
                "tickets": definition.tickets,
            }
        )
