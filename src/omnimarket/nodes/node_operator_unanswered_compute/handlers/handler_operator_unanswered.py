# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure structural coverage. Assistant tool calls are never an answer."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import unquote

from omnimarket.handlers.operator_messages import (
    content_digest,
    message_text,
    operator_message_id,
    record_identity,
    record_native_candidates,
    structural_messages,
)
from omnimarket.models.operator_unanswered import (
    ModelMessageArrival,
    ModelUnansweredMessage,
    ModelUnansweredRequest,
    ModelUnansweredResult,
)


def row_cells(row: str) -> dict[str, str]:
    return {
        key: unquote(value)
        for cell in row.split(" | ")[2:]
        if "=" in cell
        for key, value in [cell.split("=", 1)]
    }


class HandlerOperatorUnanswered:
    def handle(self, request: ModelUnansweredRequest) -> ModelUnansweredResult:
        arrivals = {
            a.message_id: a
            for a in request.arrivals
            if a.session_id == request.session_id
        }
        replies: set[str] = set()
        for row in request.item_rows:
            cells = row_cells(row)
            if cells.get("session") != request.session_id:
                continue
            if cells.get("kind") == "operator-message-arrival":
                arrival = ModelMessageArrival(
                    message_id=cells["item"],
                    session_id=request.session_id,
                    arrived_at=datetime.fromisoformat(
                        row.split(" | ", 1)[0].replace("Z", "+00:00")
                    ),
                    native_id=cells.get("native_id", ""),
                    content_sha256=cells.get("content_sha256", ""),
                )
                arrivals.setdefault(arrival.message_id, arrival)
            elif cells.get("kind") == "operator-message-reply":
                replies.update(filter(None, cells.get("answers", "").split(",")))
        records = tuple(
            r.model_dump(mode="json", exclude_none=True, by_alias=True)
            for r in request.records
        )
        pending, covered = structural_messages(records)
        matched: set[str] = set()
        covered_indices = {index for index, _ in covered}
        result: dict[str, ModelUnansweredMessage] = {}
        for index, record in sorted([*pending, *covered], key=lambda pair: pair[0]):
            native_ids = record_native_candidates(records, index)
            native_id = record_identity(record, index)
            record_stamp = datetime.fromisoformat(
                str(record.get("timestamp") or request.now.isoformat()).replace(
                    "Z", "+00:00"
                )
            )
            text = message_text(record)
            message_id = operator_message_id(request.session_id, native_id)
            match = next(
                (
                    a
                    for a in arrivals.values()
                    if a.message_id not in matched
                    and (a.message_id == message_id or a.native_id in native_ids)
                ),
                None,
            )
            if match is None:
                match = next(
                    (
                        a
                        for a in arrivals.values()
                        if a.message_id not in matched
                        and not a.native_id
                        and record_stamp >= a.arrived_at
                        and a.content_sha256 == content_digest(text)
                    ),
                    None,
                )
            if match is not None:
                message_id = match.message_id
                stamp = match.arrived_at
                matched.add(message_id)
            else:
                stamp = datetime.fromisoformat(
                    str(record.get("timestamp") or request.now.isoformat()).replace(
                        "Z", "+00:00"
                    )
                )
            if index in covered_indices:
                replies.add(message_id)
            else:
                result[message_id] = ModelUnansweredMessage(
                    message_id=message_id,
                    session_id=request.session_id,
                    arrived_at=stamp,
                    age_seconds=max(0.0, (request.now - stamp).total_seconds()),
                    words=text,
                )
        for arrival in arrivals.values():
            if arrival.message_id not in matched:
                result[arrival.message_id] = ModelUnansweredMessage(
                    message_id=arrival.message_id,
                    session_id=request.session_id,
                    arrived_at=arrival.arrived_at,
                    age_seconds=max(
                        0.0, (request.now - arrival.arrived_at).total_seconds()
                    ),
                )
        return ModelUnansweredResult(
            unanswered=tuple(
                sorted(
                    (m for key, m in result.items() if key not in replies),
                    key=lambda m: (m.arrived_at, m.message_id),
                )
            ),
            answered_ids=tuple(sorted(replies)),
        )
