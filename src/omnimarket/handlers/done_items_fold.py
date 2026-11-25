# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold of work-ledger events into done items.

Shared by node_done_items_compute and the done-items projection writer, so
neither imports the other's handler package.

Pure: no clock read, no state, no I/O. Only a TERMINAL row whose outcome's first
word is ``done`` makes its tickets done (``outcome=done delegated=1`` still counts);
``handed-off``, other outcomes and STATUS rows such as a merge never do. A ticket
done twice keeps the newest done event by row timestamp, then by row id, so the
result does not depend on the order the events arrive in.
"""

from __future__ import annotations

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import ModelLedgerTerminalEvent
from omnimarket.models.done_items import (
    ModelDoneItem,
    ModelDoneItemsProjection,
    ModelDoneItemsRequest,
)

_DONE = "done"


def _is_done(event: ModelLedgerTerminalEvent) -> bool:
    words = (event.outcome or "").split()
    return bool(words) and words[0] == _DONE


def fold_done_items(request: ModelDoneItemsRequest) -> ModelDoneItemsProjection:
    """One done item per ticket, ordered by ticket, with their count."""
    newest: dict[str, ModelLedgerTerminalEvent] = {}
    for event in request.events:
        if event.row_type is not EnumLedgerRowType.TERMINAL:
            continue
        if not isinstance(event, ModelLedgerTerminalEvent) or not _is_done(event):
            continue
        for ticket in event.tickets:
            held = newest.get(ticket)
            if held is None or (event.row_timestamp, event.row_id) > (
                held.row_timestamp,
                held.row_id,
            ):
                newest[ticket] = event
    items = tuple(
        ModelDoneItem(
            ticket=ticket,
            done_at=event.row_timestamp,
            done_row_id=event.row_id,
            lane=event.row_lane,
            pr=event.pr,
            closes=event.closes,
        )
        for ticket, event in sorted(newest.items())
    )
    as_of = max((e.row_timestamp for e in request.events), default=None)
    return ModelDoneItemsProjection(items=items, done=len(items), as_of=as_of)


__all__: list[str] = ["fold_done_items"]
