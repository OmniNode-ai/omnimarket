# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold explicit arrival/reply rows without reading a clock, file, environment or bus."""

import hashlib
from datetime import UTC, datetime

from omnimarket.handlers.handler_operator_ledger import parse_ledger_row
from omnimarket.models.model_operator_message_state import (
    ModelOperatorMessageEntry,
    ModelOperatorMessageStateReport,
    ModelOperatorMessageStateRequest,
)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class HandlerOperatorMessageState:
    def handle(
        self, request: ModelOperatorMessageStateRequest
    ) -> ModelOperatorMessageStateReport:
        config = request.config
        now = _aware(request.now)
        rows = []
        for order, raw in enumerate(request.rows):
            parsed = parse_ledger_row(raw)
            if parsed is None:
                continue
            stamp, kind, _, cells = parsed
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if at <= now:
                rows.append((at, order, kind, cells, raw))
        rows.sort(key=lambda r: (r[0], r[1]))
        entries: dict[str, ModelOperatorMessageEntry] = {}
        aliases: dict[str, str] = {}
        for at, _, kind, cells, raw in rows:
            if (
                kind in config.arrival_row_kinds
                and cells.get("kind") in config.arrival_kinds
            ):
                source = next(
                    (cells[c] for c in config.source_id_cells if cells.get(c)), raw
                )
                identity = next(
                    (cells[c] for c in config.message_id_cells if cells.get(c)),
                    config.item_prefix
                    + hashlib.sha256(source.encode()).hexdigest()[: config.id_chars],
                )
                if identity in entries:
                    continue
                entries[identity] = ModelOperatorMessageEntry(
                    item_id=identity,
                    source_id=source,
                    owner_lane=cells.get("owner")
                    or cells.get("lane")
                    or config.default_owner,
                    arrived_at=at,
                    state=config.unanswered_state,
                    age_minutes=(now - at).total_seconds() / 60,
                )
                for alias in (
                    identity,
                    source,
                    *(cells[c] for c in config.source_id_cells if cells.get(c)),
                ):
                    aliases[alias] = identity
            elif kind in config.reply_row_kinds and (
                config.operator_recipient in cells.get("to", "").split(",")
                or cells.get("kind") in config.reply_kinds
            ):
                refs = [
                    ref.strip()
                    for cell in config.answer_cells
                    for ref in cells.get(cell, "").split(",")
                    if ref.strip()
                ]
                for ref in refs:
                    target = aliases.get(ref)
                    if target is None or entries[target].state == config.answered_state:
                        continue
                    entry = entries[target]
                    entries[target] = entry.model_copy(
                        update={
                            "state": config.answered_state,
                            "answered_at": at,
                            "evidence": (raw,),
                        }
                    )
        items = tuple(sorted(entries.values(), key=lambda e: (e.arrived_at, e.item_id)))
        return ModelOperatorMessageStateReport(
            generated_at=now,
            items=items,
            unanswered=tuple(
                e.item_id for e in items if e.state == config.unanswered_state
            ),
        )
