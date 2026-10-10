# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One provider call of the effect, reported while the effect is still running."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


__all__ = ["ModelLlmDelegationCallObservation"]
