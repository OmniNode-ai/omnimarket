# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unowned reading (plan task 7.1, the G1 part): FINDING, UNREAD or OK, appended as a STATUS row (OMN-25981).

FINDING when an item that is not done has had no running lane for longer than the overlay's bound; UNREAD when the
rows or the plan owner check could not be read (an unread source never proves an item owned or unowned); OK
otherwise. The row is written by the node's own identity. Exit codes: 0 OK, 1 FINDING, 2 UNREAD.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_item_fold import (
    fold_items,
    moment,
    stamp_of,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
    EnumItemState,
    EnumReadingVerdict,
    ModelUnownedReading,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerOverlay,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.protocols import (
    LedgerUnreadableError,
    PlanOwnerUnreadableError,
    ProtocolLedgerRows,
    ProtocolPlanOwnerMissing,
    ProtocolRowAppender,
    RowAppendFailedError,
)

EXIT_CODES = {
    EnumReadingVerdict.OK: 0,
    EnumReadingVerdict.FINDING: 1,
    EnumReadingVerdict.UNREAD: 2,
}


class HandlerUnownedReading:
    """``read() -> reading``: fold the items and report the ones unowned past the bound."""

    def __init__(
        self,
        *,
        overlay: ModelItemOwnerOverlay,
        rows: ProtocolLedgerRows,
        plan_owner: ProtocolPlanOwnerMissing,
        appender: ProtocolRowAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._rows = rows
        self._plan_owner = plan_owner
        self._appender = appender
        self._clock = clock

    def read(self) -> ModelUnownedReading:
        policy = self._overlay.policy
        now = stamp_of(self._clock().astimezone(UTC))
        bound = timedelta(minutes=policy.owner_after_minutes)
        try:
            rows = self._rows.read()
            rules = self._plan_owner.read()
        except (LedgerUnreadableError, PlanOwnerUnreadableError) as exc:
            reading = ModelUnownedReading(
                verdict=EnumReadingVerdict.UNREAD,
                read_at=now,
                bound_minutes=policy.owner_after_minutes,
                items_read=0,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return self._append(reading)
        folded = fold_items(rows, rules, now, policy)
        at = moment(now)
        unowned = tuple(
            item.item_id
            for item in sorted(
                folded.items, key=lambda item: (item.unowned_since, item.item_id)
            )
            if item.state is EnumItemState.OPEN
            and at - moment(item.unowned_since) > bound
        )
        reading = ModelUnownedReading(
            verdict=EnumReadingVerdict.FINDING if unowned else EnumReadingVerdict.OK,
            read_at=now,
            bound_minutes=policy.owner_after_minutes,
            items_read=len(folded.items),
            unowned=unowned,
            detail=(
                f"{len(unowned)} of {len(folded.items)} items have had no owner for more than "
                f"{policy.owner_after_minutes:g} minutes"
            ),
        )
        return self._append(reading)

    def _append(self, reading: ModelUnownedReading) -> ModelUnownedReading:
        policy = self._overlay.policy
        listed = ",".join(reading.unowned[: policy.reading_id_limit]) or "none"
        row = " | ".join(
            [
                reading.read_at,
                "STATUS",
                f"lane={policy.node_lane}",
                f"actor={policy.node_actor}",
                "model=none",
                f"ticket={self._overlay.dispatch.ticket}",
                "check=item-unowned",
                f"verdict={reading.verdict.value}",
                f"unowned={len(reading.unowned)}",
                f"items_read={reading.items_read}",
                f"bound_minutes={reading.bound_minutes:g}",
                f"items={listed}",
                " ".join(reading.detail.replace("|", " ").split())[:400] or "no detail",
            ]
        )
        if self._appender is None:
            return reading.model_copy(update={"status_row": row})
        try:
            self._appender.append(row)
        except RowAppendFailedError as exc:
            return reading.model_copy(
                update={
                    "status_row": "",
                    "detail": f"{reading.detail}; row not appended: {exc}",
                }
            )
        return reading.model_copy(update={"status_row": row})


__all__ = ["EXIT_CODES", "HandlerUnownedReading"]
