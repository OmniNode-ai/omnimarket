# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_push_validation_effect — push-validation EFFECT (gateway P2 tenant #1).

OMN-14920: contract + models + def-B handler
(``HandlerPushValidationEffect``, operation ``run_push_validation``). The
handler is hand-written under the documented-exception grant of 2026-07-22
(RSD Track-2 generation not usable this week); the contract + acceptance suite
in tests/nodes/node_push_validation_effect/ are the future RSD regeneration
target.

OMN-16524 (rung R1): a SECOND def-B handler, ``HandlerSuiteEvaluationEffect``
(operation ``run_suite_evaluation``), added to this same contract package —
see that handler module's docstring for the extend-vs-net-new reasoning.
"""

from omnimarket.nodes.node_push_validation_effect.handlers.handler_push_validation_effect import (
    HandlerPushValidationEffect,
)
from omnimarket.nodes.node_push_validation_effect.handlers.handler_suite_evaluation_effect import (
    HandlerSuiteEvaluationEffect,
)


class NodePushValidationEffect(HandlerPushValidationEffect):
    """ONEX entry-point wrapper for HandlerPushValidationEffect (OMN-14920)."""


__all__ = [
    "HandlerPushValidationEffect",
    "HandlerSuiteEvaluationEffect",
    "NodePushValidationEffect",
]
