# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic whole-list credential-route write capability at the database boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_credential_route_write import ModelCredentialRouteWrite


@runtime_checkable
class ProtocolCredentialRouteWrite(Protocol):
    def replace_credential_route(
        self,
        plan: ModelCredentialRouteWrite,
    ) -> tuple[bool, int | None]:
        """Return whether written and the resulting/current overlay version."""
        ...
