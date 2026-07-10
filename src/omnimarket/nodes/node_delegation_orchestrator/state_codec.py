# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Durable-state codec for ``DelegationWorkflowState`` (OMN-14208).

``DelegationWorkflowState`` is a stdlib ``@dataclass`` with nested Pydantic
models, a UUID, an enum, and lists (``handler_delegation_workflow.py:646``) —
it has no ``model_dump()`` / ``model_validate_json()``. ``pydantic.TypeAdapter``
is the correct codec for a plain dataclass built from Pydantic-typed fields;
``dataclasses.asdict()`` or a bare ``json.dumps()`` would not round-trip the
nested Pydantic models, UUID, enum, or datetime fields losslessly.

This module is the sole encode/decode surface a durable ``state_io`` binding
uses to persist and reload workflow state across process boundaries — the
runtime dispatch-seam boundary hook loads state before ``handle()`` and
persists it after, so a workflow's FSM state survives a cold-process replay
instead of living only in the in-process ``_shared_workflows`` dict.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
)

_ADAPTER: TypeAdapter[DelegationWorkflowState] = TypeAdapter(DelegationWorkflowState)


def encode(state: DelegationWorkflowState) -> bytes:
    """Serialize workflow state to JSON bytes for durable storage."""
    return _ADAPTER.dump_json(state)


def decode(raw: bytes | str) -> DelegationWorkflowState:
    """Deserialize durably-stored JSON back into workflow state."""
    return _ADAPTER.validate_json(raw)


__all__: list[str] = ["decode", "encode"]
