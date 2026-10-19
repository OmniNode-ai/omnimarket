# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One provider call of the effect, reported while the effect is still running."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_secret_source import EnumSecretSource


class ModelLlmDelegationCallObservation(BaseModel):
    """A provider call that started or finished inside one effect invocation.

    The effect returns its result only when every call it makes is over. A
    caller that cancels it first (the handler's execution budget) would
    otherwise know nothing of the calls already made: which model, which key
    source, and what the provider answered. ``started`` is reported once the
    key has resolved and the request is about to leave; ``finished`` when the
    call is over, with the provider's answer. Never carries a secret value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["started", "finished"]
    model_id: str = Field(min_length=1)
    secret_source: EnumSecretSource | None = None
    success: bool | None = None
    failure_class: EnumDelegationFailureClass | None = None
    http_status: int | None = None
    error_message: str = ""
    # OMN-20910: the attempt's place in its invocation (1-based, in the order
    # the effect made them) and, on ``finished``, what the provider reported.
    # These are what a cost record is built from, so an attempt made before a
    # cancellation is recorded from its own observation, not from a result the
    # cancelled caller never receives. ``None`` is "the provider said nothing".
    attempt: int = Field(default=1, ge=1)
    served_model_id: str | None = None
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_5m_tokens: int | None = Field(default=None, ge=0)
    cache_write_1h_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)

    # OMN-20910: the native call's stop reason and measured cache usage, on a
    # ``finished`` observation, so a cost event built per attempt sees them even
    # when the effect is cancelled before it returns.
    stop_reason: EnumAnthropicStopReason | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


__all__ = ["ModelLlmDelegationCallObservation"]
