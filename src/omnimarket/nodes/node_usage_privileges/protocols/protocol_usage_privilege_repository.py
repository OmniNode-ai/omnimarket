# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the usage privilege catalog is read from."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_usage_privileges.models.model_usage_privilege import (
    ModelUsagePrivilege,
)


@runtime_checkable
class ProtocolUsagePrivilegeRepository(Protocol):
    """Read every usage privilege ordered by plan tier code, then resource code."""

    async def fetch_usage_privileges(self) -> Sequence[ModelUsagePrivilege]: ...


__all__: list[str] = ["ProtocolUsagePrivilegeRepository"]
