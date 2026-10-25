# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stamp the result and TERMINAL from the same actual executor facts."""

import re

from ..models.model_execution_attribution import (
    ModelExecutionAttributionRequest,
    ModelExecutionAttributionResult,
)


class HandlerExecutionAttribution:
    def handle(
        self, request: ModelExecutionAttributionRequest
    ) -> ModelExecutionAttributionResult:
        cells = request.cells()
        if request.prs:
            prs = sorted(set(request.prs))
            if any(
                re.fullmatch(r"(?:[\w.-]+/)?[\w.-]+#[0-9]+", pr) is None for pr in prs
            ):
                raise ValueError(
                    "attribution PR must be repo#number or owner/repo#number"
                )
            cells["pr"] = ",".join(prs)
        return ModelExecutionAttributionResult(
            result={**request.result, **cells}, terminal_cells=cells
        )
