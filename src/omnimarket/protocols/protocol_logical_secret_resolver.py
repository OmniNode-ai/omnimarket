# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime secret resolver, asked for a secret by logical name."""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr


class ProtocolLogicalSecretResolver(Protocol):
    """The runtime secret resolver, asked for a secret by logical name."""

    async def get_secret_async(
        self, logical_name: str, required: bool = True
    ) -> SecretStr | None: ...
