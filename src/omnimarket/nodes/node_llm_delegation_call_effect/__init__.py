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

from importlib import import_module
from typing import TYPE_CHECKING, Any

# OMN-26336: the public names resolve on first access. Importing one submodule of
# this package (the transport module, for instance) no longer imports every
# handler on the importing thread, which held the runtime event loop at boot.
_EXPORTS: dict[str, str] = {
    "HandlerAgentSession": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_agent_session",
    "HandlerAntigravityArguments": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerClaudeCodeArguments": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerClaudeCredentialProbe": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_credential_probe",
    "HandlerClaudeGlmArguments": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerCodexArguments": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerCodexHarnessAttempt": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt",
    "HandlerDelegationModelCall": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerDelegationTarget": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target",
    "HandlerHarnessCostPublisher": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation",
    "HandlerHarnessDelegation": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation",
    "HandlerHarnessEvalProcess": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_eval_process",
    "HandlerLabJobDelegation": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation",
    "HandlerLlmDelegationCall": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call",
    "ModelClaudeCredentialProbeReceipt": "omnimarket.models.delegation.model_claude_credential_probe",
    "ModelClaudeCredentialProbeRequest": "omnimarket.models.delegation.model_claude_credential_probe",
    "ModelCodexHarnessPolicy": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt",
    "ModelCodexHarnessReceipt": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt",
    "ModelCodexHarnessRequest": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt",
    "ModelLlmDelegationCallObservation": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation",
    "ModelLlmDelegationCallRequest": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request",
    "ModelLlmDelegationCallResult": "omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result",
    "NodeLlmDelegationCallEffect": "omnimarket.nodes.node_llm_delegation_call_effect.node",
    "cloud_start_script": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_cloud_session_start",
    "current_call_observer": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call",
    "current_local_model_entry": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call",
    "post_chat_completion": "omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport",
}

if TYPE_CHECKING:
    from omnimarket.models.delegation.model_claude_credential_probe import (
        ModelClaudeCredentialProbeReceipt as ModelClaudeCredentialProbeReceipt,
    )
    from omnimarket.models.delegation.model_claude_credential_probe import (
        ModelClaudeCredentialProbeRequest as ModelClaudeCredentialProbeRequest,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_agent_session import (
        HandlerAgentSession as HandlerAgentSession,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_credential_probe import (
        HandlerClaudeCredentialProbe as HandlerClaudeCredentialProbe,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_cloud_session_start import (
        cloud_start_script as cloud_start_script,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt import (
        HandlerCodexHarnessAttempt as HandlerCodexHarnessAttempt,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerAntigravityArguments as HandlerAntigravityArguments,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerClaudeCodeArguments as HandlerClaudeCodeArguments,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerClaudeGlmArguments as HandlerClaudeGlmArguments,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerCodexArguments as HandlerCodexArguments,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerDelegationModelCall as HandlerDelegationModelCall,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerDelegationTarget as HandlerDelegationTarget,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation import (
        HandlerHarnessCostPublisher as HandlerHarnessCostPublisher,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation import (
        HandlerHarnessDelegation as HandlerHarnessDelegation,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_eval_process import (
        HandlerHarnessEvalProcess as HandlerHarnessEvalProcess,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
        HandlerLabJobDelegation as HandlerLabJobDelegation,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        HandlerLlmDelegationCall as HandlerLlmDelegationCall,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        current_call_observer as current_call_observer,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        current_local_model_entry as current_local_model_entry,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
        post_chat_completion as post_chat_completion,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
        ModelCodexHarnessPolicy as ModelCodexHarnessPolicy,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
        ModelCodexHarnessReceipt as ModelCodexHarnessReceipt,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_codex_harness_attempt import (
        ModelCodexHarnessRequest as ModelCodexHarnessRequest,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
        ModelLlmDelegationCallObservation as ModelLlmDelegationCallObservation,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
        ModelLlmDelegationCallRequest as ModelLlmDelegationCallRequest,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
        ModelLlmDelegationCallResult as ModelLlmDelegationCallResult,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.node import (
        NodeLlmDelegationCallEffect as NodeLlmDelegationCallEffect,
    )


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "HandlerAgentSession",
    "HandlerAntigravityArguments",
    "HandlerClaudeCodeArguments",
    "HandlerClaudeCredentialProbe",
    "HandlerClaudeGlmArguments",
    "HandlerCodexArguments",
    "HandlerCodexHarnessAttempt",
    "HandlerDelegationModelCall",
    "HandlerDelegationTarget",
    "HandlerHarnessCostPublisher",
    "HandlerHarnessDelegation",
    "HandlerHarnessEvalProcess",
    "HandlerLabJobDelegation",
    "HandlerLlmDelegationCall",
    "ModelClaudeCredentialProbeReceipt",
    "ModelClaudeCredentialProbeRequest",
    "ModelCodexHarnessPolicy",
    "ModelCodexHarnessReceipt",
    "ModelCodexHarnessRequest",
    "ModelLlmDelegationCallObservation",
    "ModelLlmDelegationCallRequest",
    "ModelLlmDelegationCallResult",
    "NodeLlmDelegationCallEffect",
    "cloud_start_script",
    "current_call_observer",
    "current_local_model_entry",
    "post_chat_completion",
]
