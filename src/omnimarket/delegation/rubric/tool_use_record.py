# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tool_use verdict record of one scored run, with no I/O (OMN-20233, OMN-20290).

``tool_use_score`` reads a session store or a stream file and writes the record;
the delegated code edit loop builds the request from its own turns. Both call
``score`` here, so the loop never imports the reader.
"""

from __future__ import annotations

from omnimarket.delegation.rubric.attempt_verdict import attempt_verdict_from
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelRubricCheckRequest,
)

RECORD_SCHEMA = "tool-use-rubric-verdict.v1"


def score(
    request: ModelRubricCheckRequest, source: str, run_ref: str
) -> dict[str, object]:
    """The verdict record of one scored run."""
    verdict = HandlerDelegationRubricCheck().handle(request)
    transcript = request.transcript
    assert transcript is not None
    return {
        "schema": RECORD_SCHEMA,
        "source": source,
        "run_ref": run_ref,
        "verdict": verdict.model_dump(mode="json"),
        "attempt_verdict": attempt_verdict_from(verdict).model_dump(mode="json"),
        "measured": {
            "turns": transcript.turn_count,
            "tool_calls": len(transcript.tool_calls),
            "wall_time_ms": transcript.wall_time_ms,
        },
        "inputs": {
            "declared_tools": len(transcript.declared_tools),
            "workspace_manifest": "absent"
            if transcript.workspace_files is None
            else f"{len(transcript.workspace_files)} files",
            "execution_results": [
                row.model_dump(mode="json") for row in request.execution_results
            ],
        },
    }
