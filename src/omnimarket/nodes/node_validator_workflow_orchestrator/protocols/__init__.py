# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected effects of node_validator_workflow_orchestrator (OMN-23078).

Reading the change, emitting the bus trace and posting the gate are effects; the validators
are pure compute nodes resolved from their own contracts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    ModelChangeRead,
    ModelGateDecision,
    ModelValidatorWorkflowRequest,
)


@runtime_checkable
class ProtocolChangeReader(Protocol):
    def read_change(
        self, request: ModelValidatorWorkflowRequest
    ) -> ModelChangeRead: ...

    def validator_input(
        self,
        request: ModelValidatorWorkflowRequest,
        input_kind: str,
        validation_contract: object,
    ) -> BaseModel: ...


@runtime_checkable
class ProtocolTraceEmitter(Protocol):
    def emit(self, topic: str, payload: dict[str, object]) -> None: ...


@runtime_checkable
class ProtocolCheckGate(Protocol):
    def post(self, decision: ModelGateDecision) -> None: ...


@runtime_checkable
class ProtocolContractSource(Protocol):
    """Reads a validator's node contract and validation contract as YAML documents."""

    def node_contract(self, node_package: str) -> dict[str, object]: ...

    def validation_contract(
        self, package: str, file_name: str
    ) -> dict[str, object]: ...


__all__ = [
    "ProtocolChangeReader",
    "ProtocolCheckGate",
    "ProtocolContractSource",
    "ProtocolTraceEmitter",
]
