# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map one record of the agent message topic to the row it writes.

Every record becomes a row. A readable message is an ``ok`` row; a record with nothing in it (dropped), bytes that are not
a JSON object (unreadable) or a message the shared agent message model refuses (refused) is a ``refused`` row that names
the reason, so no record disappears without a count. The sender is never marked verified: the sender proof is not
deployed (contract ``sender_proof.deployed``), and the wire message has no field a proof could be read from.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError

from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.nodes.node_projection_agent_message.models import (
    EnumAgentMessageRefusalReason,
    EnumAgentMessageRowStatus,
    ModelAgentMessageDelivery,
    ModelAgentMessageRow,
)

_WIRE_FIELDS: Final[tuple[str, ...]] = (
    "from",
    "to",
    "thread",
    "kind",
    "text",
    "sent_at",
    "platform",
)
MAX_EXCERPT_CHARS: Final[int] = 2000


def _now() -> datetime:
    return datetime.now(UTC)


class HandlerProjectionAgentMessage:
    """Validate one delivery and return its row."""

    def __init__(
        self,
        clock: Callable[[], datetime] = _now,
        dlq_topics: Sequence[str] = (),
    ) -> None:
        self._clock = clock
        self._dlq_topics = tuple(dlq_topics)

    def dlq_envelope(
        self, request: ModelAgentMessageDelivery, row: ModelAgentMessageRow
    ) -> tuple[str, bytes] | None:
        """The dead-letter copy of a refused row: the contract's dlq_topics entry and the record's place and reason; None for a readable message."""
        if row.status is EnumAgentMessageRowStatus.OK or not self._dlq_topics:
            return None
        record = {
            "topic": request.topic,
            "partition": request.partition,
            "offset": request.offset,
            "refusal_reason": row.refusal_reason.value if row.refusal_reason else None,
            "refusal_detail": row.refusal_detail,
            "excerpt": row.text,
        }
        return self._dlq_topics[0], json.dumps(record, sort_keys=True).encode()

    def handle(self, request: ModelAgentMessageDelivery) -> ModelAgentMessageRow:
        decoded: object = request.payload
        if decoded is None and request.raw is not None:
            if not request.raw:
                return self._refused(request, EnumAgentMessageRefusalReason.DROPPED)
            try:
                decoded = json.loads(request.raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                excerpt = request.raw.decode("utf-8", errors="replace")[
                    :MAX_EXCERPT_CHARS
                ]
                return self._refused(
                    request,
                    EnumAgentMessageRefusalReason.UNREADABLE,
                    detail="the record is not JSON",
                    text=excerpt,
                )
        if decoded is None or decoded == {}:
            return self._refused(request, EnumAgentMessageRefusalReason.DROPPED)
        if not isinstance(decoded, dict):
            return self._refused(
                request,
                EnumAgentMessageRefusalReason.UNREADABLE,
                detail="the record is not a JSON object",
                text=str(decoded)[:MAX_EXCERPT_CHARS],
            )
        inner = decoded.get("payload", decoded)
        if not isinstance(inner, dict):
            return self._refused(
                request,
                EnumAgentMessageRefusalReason.UNREADABLE,
                detail="the payload is not a JSON object",
            )
        try:
            message = ModelAgentMessage.model_validate(
                {key: inner[key] for key in _WIRE_FIELDS if key in inner}
            )
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'message'}: {err['msg']}"
                for err in exc.errors()
            )
            return self._refused(
                request, EnumAgentMessageRefusalReason.REFUSED, detail=detail
            )
        return ModelAgentMessageRow(
            source_partition=request.partition,
            source_offset=request.offset,
            status=EnumAgentMessageRowStatus.OK,
            from_agent=message.sender,
            to_agent=message.to,
            thread=message.thread,
            kind=message.kind.value,
            platform=message.platform.value,
            text=message.text,
            sent_at=message.sent_at,
            broker_at=request.broker_time,
            sender_verified=False,
            projected_at=self._clock(),
        )

    def _refused(
        self,
        request: ModelAgentMessageDelivery,
        reason: EnumAgentMessageRefusalReason,
        *,
        detail: str | None = None,
        text: str | None = None,
    ) -> ModelAgentMessageRow:
        return ModelAgentMessageRow(
            source_partition=request.partition,
            source_offset=request.offset,
            status=EnumAgentMessageRowStatus.REFUSED,
            refusal_reason=reason,
            refusal_detail=detail,
            text=text,
            broker_at=request.broker_time,
            projected_at=self._clock(),
        )


__all__ = ["MAX_EXCERPT_CHARS", "HandlerProjectionAgentMessage"]
