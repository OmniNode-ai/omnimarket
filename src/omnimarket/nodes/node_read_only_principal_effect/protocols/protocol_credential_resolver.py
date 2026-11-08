# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The boundary that answers whether a credential reference resolves in its declared store."""

from __future__ import annotations

from typing import Protocol

from ..models.model_read_only_principal import ModelCredentialRef


class ProtocolCredentialResolver(Protocol):
    """Say whether the reference names a stored credential. The value is never returned or reported."""

    def resolves(self, reference: ModelCredentialRef) -> bool: ...
