# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decision table classifier — maps one polish signal to exactly one task class.

Decision table (§3.2 of design doc). Single-action policy; first match wins.
All STUCK cases return confidence=1.0; actionable cases carry lower confidence.

No LLM calls. Pure deterministic logic.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass
from omnimarket.nodes.node_polish_task_classifier.models.model_polish_classify_request import (
    ModelPolishClassifyRequest,
)
from omnimarket.nodes.node_polish_task_classifier.models.model_polish_classify_result import (
    ModelPolishClassifyResult,
)

logger = logging.getLogger(__name__)

# Dependency-change patterns that force STUCK on ci_log
_DEP_CHANGE_PATTERNS: frozenset[str] = frozenset(
    {"pyproject.toml", "uv.lock", "requirements", "package.json"}
)

_CONFLICT_MARKER = "<<<<<<<"


def _classify(req: ModelPolishClassifyRequest) -> ModelPolishClassifyResult:
    signals = sum(
        [
            req.thread_body is not None,
            req.conflict_hunk is not None,
            req.ci_log is not None,
        ]
    )

    # Multiple signals simultaneously → STUCK
    if signals > 1:
        return ModelPolishClassifyResult(
            task_class=EnumPolishTaskClass.STUCK,
            confidence=1.0,
            reason="ambiguous: multiple signals",
        )

    # No signals → STUCK
    if signals == 0:
        return ModelPolishClassifyResult(
            task_class=EnumPolishTaskClass.STUCK,
            confidence=1.0,
            reason="no signal",
        )

    # --- thread_body branch ---
    if req.thread_body is not None:
        if len(req.thread_body) >= 2000:
            return ModelPolishClassifyResult(
                task_class=EnumPolishTaskClass.STUCK,
                confidence=1.0,
                reason="thread too long",
            )
        return ModelPolishClassifyResult(
            task_class=EnumPolishTaskClass.THREAD_REPLY,
            confidence=0.8,
            reason="thread_body present and within length limit",
        )

    # --- conflict_hunk branch ---
    if req.conflict_hunk is not None:
        if _CONFLICT_MARKER not in req.conflict_hunk:
            return ModelPolishClassifyResult(
                task_class=EnumPolishTaskClass.STUCK,
                confidence=1.0,
                reason="multi-file or ambiguous conflict",
            )
        # Detect multi-file: more than one occurrence of the conflict marker
        # indicates merged hunks from multiple files.
        marker_count = req.conflict_hunk.count(_CONFLICT_MARKER)
        if marker_count > 1:
            return ModelPolishClassifyResult(
                task_class=EnumPolishTaskClass.STUCK,
                confidence=1.0,
                reason="multi-file or ambiguous conflict",
            )
        return ModelPolishClassifyResult(
            task_class=EnumPolishTaskClass.CONFLICT_HUNK,
            confidence=0.9,
            reason="single-file conflict hunk with markers",
        )

    # --- ci_log branch ---
    if req.ci_log is not None:
        if len(req.ci_log) >= 20_000:
            return ModelPolishClassifyResult(
                task_class=EnumPolishTaskClass.STUCK,
                confidence=1.0,
                reason="ci_log too large",
            )
        for pattern in _DEP_CHANGE_PATTERNS:
            if pattern in req.ci_log:
                return ModelPolishClassifyResult(
                    task_class=EnumPolishTaskClass.STUCK,
                    confidence=1.0,
                    reason=f"dep-change pattern detected: {pattern}",
                )
        return ModelPolishClassifyResult(
            task_class=EnumPolishTaskClass.CI_FIX,
            confidence=0.7,
            reason="ci_log present, within size limit, no dep-change patterns",
        )

    # Unreachable — all signal branches covered above
    return ModelPolishClassifyResult(
        task_class=EnumPolishTaskClass.STUCK,
        confidence=1.0,
        reason="no signal",
    )


class HandlerPolishTaskClassifier:
    """Deterministic decision table handler. No LLM calls."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        request: ModelPolishClassifyRequest,
    ) -> ModelPolishClassifyResult:
        result = _classify(request)
        logger.info(
            "classify pr=%d repo=%s -> %s (confidence=%.2f, reason=%r)",
            request.pr_number,
            request.repo,
            result.task_class,
            result.confidence,
            result.reason,
        )
        return result
