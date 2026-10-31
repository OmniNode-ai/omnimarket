# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic credential-row write capability at the database boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_credential_row_write import ModelCredentialRowWrite


@runtime_checkable
class ProtocolCredentialRowWrite(Protocol):
    def write_credential_row(self, plan: ModelCredentialRowWrite) -> int | None:
        """Run the plan's one statement; return the row's version after it."""
        ...
