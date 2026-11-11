# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record one lab proof judge use: one event on the bus and one ledger row."""

from __future__ import annotations

import json
import uuid
from datetime import UTC

from omnimarket.nodes.node_lab_proof_judge_use_effect.models.model_lab_proof_judge_use_record import (
    ModelLabProofJudgeUseRecord,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.models.model_lab_proof_judge_use_result import (
    ModelLabProofJudgeUseResult,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    ModelLabProofJudgeUseOverlay,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.protocols import (
    ProtocolUseEventPublisher,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

_ROW_TYPE = "STATUS"


class HandlerLabProofJudgeUse:
    """Records a typed use record as one event and one ledger row through injected ports."""

    def __init__(
        self,
        overlay: ModelLabProofJudgeUseOverlay,
        host: str,
        appender: ProtocolReadingAppender | None,
        publisher: ProtocolUseEventPublisher | None,
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._appender = appender
        self._publisher = publisher

    def handle(
        self, request: ModelLabProofJudgeUseRecord
    ) -> ModelLabProofJudgeUseResult:
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{request.repo}#{request.pr_number}|{request.head}|{request.class_id}|{request.outcome.value}",
            )
        )

        event_published = False
        if self._publisher is not None:
            payload = json.loads(request.model_dump_json())
            event_published = self._publisher.publish(
                topic=self._overlay.event.topic,
                event_type=self._overlay.event.event_type,
                payload=payload,
                event_id=event_id,
                correlation_id=event_id,
                partition_key=f"{request.repo}#{request.pr_number}",
            )

        stamp = request.observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        substitute = ",".join(request.receipt_ids) if request.receipt_ids else "none"
        nodes = ",".join(request.nodes)
        cells = [
            f"lane={self._overlay.ledger.lane}",
            f"actor={self._overlay.ledger.actor}",
            "model=none",
            f"host={self._host}",
            f"ticket={self._overlay.ledger.ticket}",
            f"kind={self._overlay.ledger.kind_cell}",
            f"pr={request.repo}#{request.pr_number}",
            f"head={request.head}",
            f"class={request.class_id}",
            f"outcome={request.outcome.value}",
            f"substitute={substitute}",
            f"nodes={nodes}",
        ]
        row = f"{stamp} | {_ROW_TYPE} | " + " | ".join(cells)

        if self._appender is None:
            return ModelLabProofJudgeUseResult(
                event_published=event_published,
                ledger="not-requested",
                row=row,
                event_id=event_id,
            )

        # one ledger request per judgement: a repeat of the same use at the same instant is the same request, a later
        # judgement of the same change is a use of its own and a row of its own
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{event_id}|{stamp}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return ModelLabProofJudgeUseResult(
                event_published=event_published,
                ledger="append-failed",
                row=row,
                event_id=event_id,
            )
        return ModelLabProofJudgeUseResult(
            event_published=event_published,
            ledger="written" if appended.status == "accepted" else "already-written",
            row=row,
            event_id=event_id,
        )


__all__ = ["HandlerLabProofJudgeUse"]
