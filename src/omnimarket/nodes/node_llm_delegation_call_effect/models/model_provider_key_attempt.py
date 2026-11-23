# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One house key tried for a provider call, named by reference (OMN-19432)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


class ModelProviderKeyAttempt(BaseModel):
    """A key NAME and what the provider answered for it. Never a key value.

    ``skipped_cooling`` marks a key that was not called because an earlier auth
    failure still holds it out of use. ``failure_class`` is ``None`` for the key
    that answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_name: str = Field(min_length=1)
    failure_class: EnumDelegationFailureClass | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    skipped_cooling: bool = False
