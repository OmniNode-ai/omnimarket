# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What ``onex secret set`` and ``onex secret delete`` ask the local secret store to do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class ModelLocalSecretRequest(BaseModel):
    """One write to, or delete from, the local secret store.

    ``value`` is a ``SecretStr`` so the request's repr and dump never show it;
    the handler reads it once, to store it and to fingerprint it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["set", "delete"]
    secret_ref: str
    value: SecretStr | None = None
    force: bool = False
    plan: str | None = None
    model: str | None = None


__all__ = ["ModelLocalSecretRequest"]
