# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger as of an instant: typed rows, folded entity state and resolved relationships.

This module reads, it does not decide. It folds each row of the pinned prefix with the
shared work-ledger fold (the TA.5 reducer: ``fold_row``, ``apply_ops``, ``close_relationships``),
so "open", "superseded", "expired" and "corrected by" mean here exactly what they mean to
the rest of the platform. Rows stamped after ``now`` are not read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEventBase,
)
from omnimarket.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    close_relationships,
    fold_row,
    parse_stamp,
)
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.model_work_ledger_fold_result import (
    ModelWorkLedgerFoldResult,
    ModelWorkLedgerLink,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
)


@dataclass(frozen=True)
class LedgerView:
    now: datetime
    prefix_row_count: int
    refused_row_count: int
    prefix_hash: str
    # Rows at or before ``now``, in stamp order (input order within one stamp).
    events: tuple[ModelLedgerRowEventBase, ...]
    by_id: dict[str, ModelLedgerRowEventBase] = field(default_factory=dict)
    entities: dict[str, dict[str, object]] = field(default_factory=dict)


def cell(event: ModelLedgerRowEventBase, key: str) -> str | None:
    """The first ``key=value`` cell of a row, or None."""
    for item in event.cells:
        if item.key == key:
            return item.value
    return None


def split_list(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def build_view(rows: tuple[str, ...], now: datetime) -> LedgerView:
    kept = [row.strip() for row in rows if row.strip()]
    prefix_hash = hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()
    parsed: list[
        tuple[datetime, int, ModelLedgerRowEventBase, ModelWorkLedgerFoldResult]
    ] = []
    seen: set[str] = set()
    refused = 0
    for index, raw in enumerate(kept):
        try:
            folded = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
            event = parse_ledger_row(raw)
        except WorkLedgerFoldError:
            refused += 1
            continue
        if folded.row.row_id in seen:
            continue
        seen.add(folded.row.row_id)
        stamp = parse_stamp(event.row_timestamp)
        if stamp <= now:
            parsed.append((stamp, index, event, folded))
    parsed.sort(key=lambda item: (item[0], item[1]))

    state: dict[str, dict[str, object]] = {}
    links: list[ModelWorkLedgerLink] = []
    events: list[ModelLedgerRowEventBase] = []
    by_id: dict[str, ModelLedgerRowEventBase] = {}
    for _, _, event, folded in parsed:
        apply_ops(state, folded.ops)
        links.extend(folded.links)
        events.append(event)
        by_id[event.row_id] = event
    closure, _ = close_relationships(state, links, as_of=now)
    return LedgerView(
        now=now,
        prefix_row_count=len(kept),
        refused_row_count=refused,
        prefix_hash=prefix_hash,
        events=tuple(events),
        by_id=by_id,
        entities=closure,
    )
