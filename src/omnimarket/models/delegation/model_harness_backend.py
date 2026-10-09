# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""House-only harness backend declarations outside the HTTP wire config."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.enums.enum_harness import EnumHarness
from omnimarket.enums.enum_harness_surface import EnumHarnessSurface
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG


class ModelHarnessBackend(BaseModel):
    """An internal house harness and its executor binding."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    backend_id: str = Field(..., min_length=1)
    kind: Literal["harness"]
    harness: EnumHarness
    model_name: str | None
    surface: EnumHarnessSurface
    tenant: str
    executor: Literal["node_coding_agent_invoke_effect"]
    executor_bound: bool
    budget_seconds: int = Field(..., gt=0)
    terms: str = Field(..., min_length=1)

    @field_validator("tenant")
    @classmethod
    def _house_tenant_only(cls, value: str) -> str:
        if value != HOUSE_TENANT_SLUG:
            msg = f"harness tenant must equal {HOUSE_TENANT_SLUG!r}, got {value!r}"
            raise ValueError(msg)
        return value


__all__: list[str] = ["ModelHarnessBackend"]
