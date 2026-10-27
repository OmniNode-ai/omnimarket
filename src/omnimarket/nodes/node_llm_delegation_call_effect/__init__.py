# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""LLM delegation call effect node (OMN-11776).

Public surface: the canonical single-call effect handler and its typed request /
result I/O. Other nodes that compose this effect (e.g. the local in-process
delegation dispatch, OMN-13160) import from this package root — never from the
node's internal models package — so the composition stays on the node's public
boundary, not a cross-node reach-in.
"""

from __future__ import annotations

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_agent_session import (
    HandlerAgentSession,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_cloud_session_start import (
    cloud_start_script,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt import (
    HandlerCodexHarnessAttempt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerAntigravityArguments,
    HandlerClaudeCodeArguments,
    HandlerClaudeGlmArguments,
    HandlerCodexArguments,
    HandlerDelegationModelCall,
    HandlerDelegationTarget,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_eval_process import (
    HandlerHarnessEvalProcess,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
    HandlerLabJobDelegation,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
    current_call_observer,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
    ModelCodexHarnessPolicy,
    ModelCodexHarnessReceipt,
    ModelCodexHarnessRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
    ModelLlmDelegationCallObservation,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.node import (
    NodeLlmDelegationCallEffect,
)

__all__ = [
    "HandlerAgentSession",
    "HandlerAntigravityArguments",
    "HandlerClaudeCodeArguments",
    "HandlerClaudeGlmArguments",
    "HandlerCodexArguments",
    "HandlerCodexHarnessAttempt",
    "HandlerDelegationModelCall",
    "HandlerDelegationTarget",
    "HandlerHarnessEvalProcess",
    "HandlerLabJobDelegation",
    "HandlerLlmDelegationCall",
    "ModelCodexHarnessPolicy",
    "ModelCodexHarnessReceipt",
    "ModelCodexHarnessRequest",
    "ModelLlmDelegationCallObservation",
    "ModelLlmDelegationCallRequest",
    "ModelLlmDelegationCallResult",
    "NodeLlmDelegationCallEffect",
    "cloud_start_script",
    "current_call_observer",
]
