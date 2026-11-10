# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the plan catalog is read from; the hosted billing schema sits behind it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_plan_list.models.model_plan_catalog_row import (
    ModelPlanCatalogRow,
)


@runtime_checkable
class ProtocolPlanCatalogRepository(Protocol):
    """Read every plan tier left-joined to its entitlements, in catalog order."""

    async def fetch_plan_rows(self) -> Sequence[ModelPlanCatalogRow]: ...


__all__: list[str] = ["ProtocolPlanCatalogRepository"]
