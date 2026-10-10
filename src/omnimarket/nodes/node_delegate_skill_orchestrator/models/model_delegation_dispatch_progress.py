# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request-local evidence of the dispatch stage cancelled by the handler."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallObservation,
)

DispatchStage = Literal[
    "dispatch",
    "subscribe",
    "publish",
    "terminal_wait",
    "terminal_cleanup",
    "effect_boot",
    "inference",
    "quality_gate",
]


class ModelDelegationDispatchProgress(BaseModel):
    """Shared by one handler and its wait_for child, never by unrelated runs.

    Besides the stage, a dispatch port records here what it has learned so far,
    so a run the handler's budget cancels can still say where it was routed,
    which key answered and every call it made. Nothing here is a secret value.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    stage: DispatchStage = "dispatch"
    cancelled_stage: DispatchStage | None = None
    # The port's own ladder of settled attempts, in the shape it returns on its
    # terminal. The port appends to this list in place.
    attempts: list[dict[str, object]] = Field(default_factory=list)
    # The rung whose attempt has started and not yet settled: its tier,
    # backend, model, host and provider, as the settled record would name them.
    in_flight_attempt: dict[str, object] | None = None
    in_flight_endpoint_ref: str | None = None
    in_flight_secret_ref: str | None = None
    # Provider calls the running effect reported for that rung, in order.
    in_flight_calls: list[ModelLlmDelegationCallObservation] = Field(
        default_factory=list
    )
    # The key provenance the last settled attempt observed.
    secret_source: EnumSecretSource | None = None
    secret_ref: str | None = None
    escalation_count: int = 0
    cost_usd: float = 0.0
