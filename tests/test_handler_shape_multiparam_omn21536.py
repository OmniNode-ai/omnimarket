# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21536 slice 5: a handler's handle takes one typed request.

The handler-shape validator of omnibase_core judges ``handle`` of every handler
class. The fourteen handler modules below each took several positional
parameters; each now takes one typed request model. The validator itself decides
what counts, so this module holds no rule of its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_handler_shape import ValidatorHandlerShape

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"

_MULTIPARAM_HANDLERS = (
    "node_build_dispatch_effect/handlers/handler_build_dispatch.py",
    "node_closeout_effect/handlers/handler_closeout.py",
    "node_design_to_plan/handlers/handler_design_to_plan_phase3_launch.py",
    "node_handoff_effect/handlers/handler_handoff_effect.py",
    "node_monitor_alert_responder/handlers/handler_alert_classify.py",
    "node_omnigate_receipt_generator/handlers/handler_receipt_generator.py",
    "node_omnigate_receipt_verifier/handlers/handler_receipt_verifier.py",
    "node_persona_lifecycle_orchestrator/handlers/handler_persona_rebuild.py",
    "node_polish_task_classifier/handlers/handler_classifier.py",
    "node_projection_delegation/handlers/handler_delegation_data_source_fold.py",
    "node_state_persist_effect/handlers/handler_state_persist_effect.py",
    "node_thread_reply_effect/handlers/handler_thread_reply.py",
    "node_ticket_query/handlers/handler_ticket_query.py",
    "node_verify_effect/handlers/handler_verify.py",
)


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _MULTIPARAM_HANDLERS)
def test_handle_takes_one_typed_request(relative_path: str) -> None:
    result = ValidatorHandlerShape().validate(_NODES / relative_path)

    findings = [
        f"{issue.file_path}:{issue.line_number}: {issue.message}"
        for issue in result.issues
        if issue.rule_name == "noncanonical_handle"
    ]
    assert findings == []
