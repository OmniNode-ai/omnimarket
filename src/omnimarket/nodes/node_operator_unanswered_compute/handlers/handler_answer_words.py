# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prefer private handoff acknowledgement words; otherwise use capture content or request resend."""

from omnimarket.models.operator_unanswered import (
    ModelAnswerWordsRequest,
    ModelAnswerWordsResult,
)

from .handler_operator_unanswered import row_cells


class HandlerAnswerWords:
    def handle(self, request: ModelAnswerWordsRequest) -> ModelAnswerWordsResult:
        for row in reversed(request.acknowledgement_rows):
            cells = row_cells(row)
            if (
                cells.get("kind") == "operator-message-acknowledgement"
                and cells.get("item") == request.message_id
                and cells.get("words", "").strip()
            ):
                return ModelAnswerWordsResult(
                    message_id=request.message_id,
                    words=cells["words"],
                    source="acknowledgement",
                )
        if request.content_store_words and request.content_store_words.strip():
            return ModelAnswerWordsResult(
                message_id=request.message_id,
                words=request.content_store_words,
                source="content_store",
            )
        return ModelAnswerWordsResult(
            message_id=request.message_id,
            words=None,
            source="unreadable",
            reply=request.resend_text,
            leave_open=True,
            flagged=True,
        )
