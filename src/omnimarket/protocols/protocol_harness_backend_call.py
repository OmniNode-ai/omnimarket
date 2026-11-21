# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the harness backend call: the process, the answer gate and the cost record (OMN-26219)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessGateVerdict,
    ModelHarnessRunReceipt,
)


@runtime_checkable
class ProtocolHarnessRunner(Protocol):
    """Runs one prompt through the harness a backend declares, bounded by a deadline."""

    async def run(
        self,
        backend: ModelDelegationBackendConfig,
        prompt: str,
        timeout_seconds: float,
    ) -> ModelHarnessRunReceipt: ...


@runtime_checkable
class ProtocolHarnessAnswerGate(Protocol):
    """Judges one attempt's answer with the delegation answer gate."""

    def judge(
        self,
        request: ModelHarnessBackendCallRequest,
        backend: ModelDelegationBackendConfig,
        answer: str,
    ) -> ModelHarnessGateVerdict: ...


@runtime_checkable
class ProtocolHarnessCostSink(Protocol):
    """Receives the one cost record each attempt leaves."""

    def record(self, attempt: ModelHarnessAttemptRecord) -> None: ...


__all__: list[str] = [
    "ProtocolHarnessAnswerGate",
    "ProtocolHarnessCostSink",
    "ProtocolHarnessRunner",
]
