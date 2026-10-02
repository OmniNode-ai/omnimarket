# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A model a customer's route asked first and the provider could not serve (OMN-19205)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


class ModelEarlierModelAttempt(BaseModel):
    """One earlier call of the same customer route, before the model that answered.

    A customer's BYOK call re-resolves its model ONCE from the key's own list
    when the first model is throttled or its upstream is down. The result keeps
    the first call here so the receipt can record both routing attempts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(min_length=1)
    failure_class: EnumDelegationFailureClass
    error_message: str
    http_status: int | None = None
