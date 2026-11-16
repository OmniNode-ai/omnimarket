# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prepush logins overlay (OMN-23597).

Found as ``<root>/prepush_logins/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. Its ``table`` is the ``label login uv_path run_folder`` block the
full-suite host guard applies to the deployment host table; the node reads the same table
through ``login_overlay_block``, so this model validates exactly the bytes the guard parses.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers.handler_pytest_full_suite_host_guard import (
    parse_login_rows,
)


class ModelPrepushLoginsOverlay(BaseModel):
    """The prepush login table, checked by the same row rules the guard applies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    table: str = Field(min_length=1)

    @model_validator(mode="after")
    def _table_holds_rows(self) -> Self:
        if not parse_login_rows(self.table):
            raise ValueError("a prepush login overlay holds at least one row")
        return self


__all__ = [
    "ModelPrepushLoginsOverlay",
]
