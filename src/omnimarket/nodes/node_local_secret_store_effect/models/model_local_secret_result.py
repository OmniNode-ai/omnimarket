# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the local secret store did, and the metadata events the runtime folds."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
)


class ModelLocalSecretResult(BaseModel):
    """The outcome of one ``ModelLocalSecretRequest``. Never carries the value.

    ``events`` is in delivery order: a re-set's revoke of the route ref it
    replaces comes before the registration of the new one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["set", "delete"]
    secret_ref: str
    provider: str | None = None
    route_ref: str | None = None
    route_withdrawn: bool = False
    events: tuple[
        ModelCredentialRevokedEvent | ModelCredentialRegisteredEvent, ...
    ] = ()


__all__ = ["ModelLocalSecretResult"]
