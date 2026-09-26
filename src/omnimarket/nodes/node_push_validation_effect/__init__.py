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

OMN-19359 (delegated test loop T3): a THIRD def-B handler,
``HandlerFocusedTestRunEffect`` (operation ``run_focused_test_run``): one
focused pytest run in a throwaway single-mount container on a lab host.

OMN-19458: the focused run's request, receipt and client seam are exported
here because node_focused_test_run_effect, the bus-hosted form of the same
operation on the lab host, is built on them.
"""

from omnimarket.nodes.node_push_validation_effect.handlers.handler_focused_test_run_effect import (
    HandlerFocusedTestRunEffect,
)
from omnimarket.nodes.node_push_validation_effect.handlers.handler_push_validation_effect import (
    HandlerPushValidationEffect,
)
from omnimarket.nodes.node_push_validation_effect.handlers.handler_suite_evaluation_effect import (
    HandlerSuiteEvaluationEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_receipt import (
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
    ModelSourceMutation,
)
from omnimarket.nodes.node_push_validation_effect.protocols.ephemeral_container_focused_run_subprocess import (
    EphemeralContainerFocusedRunSubprocess,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
    ProtocolFocusedTestRunClient,
)


class NodePushValidationEffect(HandlerPushValidationEffect):
    """ONEX entry-point wrapper for HandlerPushValidationEffect (OMN-14920)."""


__all__ = [
    "EnumFocusedTestRunStatus",
    "EphemeralContainerFocusedRunSubprocess",
    "FocusedTestRunInfraError",
    "HandlerFocusedTestRunEffect",
    "HandlerPushValidationEffect",
    "HandlerSuiteEvaluationEffect",
    "ModelFocusedTestRunReceipt",
    "ModelFocusedTestRunRequest",
    "ModelSourceMutation",
    "NodePushValidationEffect",
    "ProtocolFocusedTestRunClient",
]
