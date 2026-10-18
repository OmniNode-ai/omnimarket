# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B Bash reduction without error-line classification or I/O.

An unstructured Bash output can contain diagnostics anywhere, including on a
successful exit. Keep every non-empty line, in order and with duplicates, and
return them as a conservative superset in error_lines. A failed command keeps
even its empty lines. Successful output may omit only long runs of empty LF
lines; spaces, carriage returns, ANSI escapes and Unicode are left alone.

No original has been stored by a pure compute. original_record_id stays null
until the store effect and its orchestration exist; there is no retrieval claim.
"""

from __future__ import annotations

import hashlib
import re

from omnimarket.nodes.node_tool_result_reduce_compute.models.model_tool_result_reduce import (
    ModelToolResultReduceRequest,
    ModelToolResultReduceResult,
)

_EMPTY_LF_RUN = re.compile(r"(?m)^\n{64,}")


class HandlerToolResultReduce:
    """Conservatively reduce a Bash result, never infer success from its text."""

    async def handle(
        self, request: ModelToolResultReduceRequest
    ) -> ModelToolResultReduceResult:
        """Return a deterministic typed result without clocks, storage or models."""
        output = request.output
        if request.exit_status == 0:
            output = _EMPTY_LF_RUN.sub(
                lambda match: f"[{len(match.group())} empty lines omitted]\n",
                output,
            )
        return ModelToolResultReduceResult(
            reduced_text=f"Bash exit_status={request.exit_status}\n{output}",
            exit_status=request.exit_status,
            error_lines=tuple(line for line in request.output.split("\n") if line),
            original_sha256=hashlib.sha256(request.output.encode("utf-8")).hexdigest(),
            original_record_id=None,
        )
