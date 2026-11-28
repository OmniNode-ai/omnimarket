# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One operation on the agent sender registry: register, list or revoke."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


class EnumAgentSenderRegistryOperation(StrEnum):
    REGISTER = "register"
    LIST = "list"
    REVOKE = "revoke"


class ModelAgentSenderRegistryRequest(BaseModel):
    """Every value comes from the request; the store folders come from the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumAgentSenderRegistryOperation
    sender: str | None = Field(
        default=None, description="The agent name a message declares in from."
    )
    principal: str | None = Field(
        default=None, description="The broker principal that agent publishes as."
    )
    key_id: str | None = Field(default=None, description="The id of the new key.")

    @model_validator(mode="after")
    def _check(self) -> Self:
        op = self.operation
        if op is EnumAgentSenderRegistryOperation.LIST:
            if any((self.sender, self.principal, self.key_id)):
                raise ValueError("list takes no sender, principal or key_id")
            return self
        if self.sender is None or not _NAME.match(self.sender):
            raise ValueError("sender is letters, digits, _ . or - (1 to 200)")
        if op is EnumAgentSenderRegistryOperation.REVOKE:
            if self.principal or self.key_id:
                raise ValueError("revoke takes the sender only")
            return self
        if not self.principal or not self.key_id:
            raise ValueError("register needs sender, principal and key_id")
        return self


__all__: list[str] = [
    "EnumAgentSenderRegistryOperation",
    "ModelAgentSenderRegistryRequest",
]
