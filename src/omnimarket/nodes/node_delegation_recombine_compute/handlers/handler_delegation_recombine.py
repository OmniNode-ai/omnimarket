# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B canonical answer join, independent of arrival order."""

from __future__ import annotations

from omnimarket.models.model_delegation_split_recombine import (
    ModelDelegationFinding,
    ModelDelegationRecombineRequest,
    ModelDelegationRecombineResult,
)


class HandlerDelegationRecombine:
    """Join in source order; duplicate locations keep the lexical first message."""

    def handle(
        self, request: ModelDelegationRecombineRequest
    ) -> ModelDelegationRecombineResult:
        answers = sorted(request.answers, key=lambda answer: answer.position)
        findings: dict[tuple[str, int], ModelDelegationFinding] = {}
        for finding in sorted(
            (finding for answer in answers for finding in answer.findings),
            key=lambda finding: (finding.path, finding.line, finding.message),
        ):
            findings.setdefault((finding.path, finding.line), finding)
        return ModelDelegationRecombineResult(
            task_id=request.task_id,
            task_class=request.task_class,
            answer="\n\n".join(answer.answer for answer in answers),
            findings=tuple(findings.values()),
        )
