# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What ``onex secret set`` and ``onex secret delete`` ask the local secret store to do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class ModelLocalSecretRequest(BaseModel):
    """One write to, or delete from, the local secret store.

    ``set_models`` (OMN-21059) replaces the ordered ``models`` of the provider
    key ``secret_ref`` names and leaves the key itself as it is.

    ``value`` is a ``SecretStr`` so the request's repr and dump never show it;
    the handler reads it once, to store it and to fingerprint it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["set", "delete", "set_models"]
    secret_ref: str
    value: SecretStr | None = None
    force: bool = False
    plan: str | None = None
    model: str | None = None
    models: tuple[str, ...] = ()
    model_chosen_by: Literal["customer", "catalogue"] | None = None


__all__ = ["ModelLocalSecretRequest"]
