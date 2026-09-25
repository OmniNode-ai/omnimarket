# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegated test control grading compute node.

Pure COMPUTE: maps the fixed-ref, pre-fix and mutation run outcomes of a
generated test to one control status. A pass at both refs is
control_did_not_fail; only an assertion-level control failure is
headline-grade (accepted_call, accepted_mutation); a collection-only
failure is accepted-weak (accepted_collection).
"""

from omnimarket.nodes.node_delegated_test_control_compute.handlers.handler_delegated_test_control import (
    HandlerDelegatedTestControl,
    grade_control,
)
from omnimarket.nodes.node_delegated_test_control_compute.models.model_delegated_test_control import (
    HEADLINE_STATUSES,
    EnumControlStatus,
    ModelControlGrade,
    ModelControlGradeRequest,
)


class NodeDelegatedTestControlCompute(HandlerDelegatedTestControl):
    """ONEX entry-point wrapper for HandlerDelegatedTestControl."""


__all__ = [
    "HEADLINE_STATUSES",
    "EnumControlStatus",
    "HandlerDelegatedTestControl",
    "ModelControlGrade",
    "ModelControlGradeRequest",
    "NodeDelegatedTestControlCompute",
    "grade_control",
]
