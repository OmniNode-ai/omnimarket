# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit one missing class through the existing queue; retain no decision state."""

import hashlib

from omnimarket.models.error_rule_match import (
    ModelErrorRuleNoMatchRequest,
    ModelErrorRuleNoMatchResult,
)
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionRow,
)
from omnimarket.protocols.protocol_error_rule_match import (
    ProtocolErrorRuleExceptionEmitter,
)

from .container import bind_exception_emit


class HandlerErrorRuleNoMatch:
    def __init__(
        self, *, emit: ProtocolErrorRuleExceptionEmitter | None = None
    ) -> None:
        self._emit = emit

    def handle(
        self, request: ModelErrorRuleNoMatchRequest
    ) -> ModelErrorRuleNoMatchResult:
        overlay = request.overlay
        key = request.decision.class_key
        cls = (
            overlay.class_prefix
            + hashlib.sha256(key.encode()).hexdigest()[: overlay.class_hash_length]
        )
        emit = (
            self._emit
            if self._emit is not None
            else bind_exception_emit(overlay.emit_node)
        )
        result = emit.handle(
            ModelExceptionEmitRequest(
                state_dir=overlay.state_dir,
                row=ModelExceptionRow(
                    cls=cls,
                    key=key,
                    source=overlay.source,
                    evidence=request.evidence,
                    tried=overlay.tried,
                    proposed_fix=overlay.proposed_fix,
                    repo=request.repository,
                    ticket=request.ticket,
                ),
                now=request.now,
                runner=overlay.runner,
                parent=overlay.parent,
                role=overlay.role,
            )
        )
        return ModelErrorRuleNoMatchResult(class_key=key, queued=result)
