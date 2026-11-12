# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the dev-broker ACL reconcile."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelBrokerAclReconcileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["dry_run", "apply"]


class ModelBrokerAclReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["dry_run", "apply"]
    exit_code: int
    declared: int | None = Field(default=None, ge=0)
    live: int | None = Field(default=None, ge=0)
    missing: int | None = Field(default=None, ge=0)
    undeclared: int | None = Field(default=None, ge=0)
    missing_rows: tuple[str, ...] = ()
    # Rows derived from contract topics for the declared principals, and topics that still have no row (None: not derived).
    derived: int | None = Field(default=None, ge=0)
    uncovered_topics: int | None = Field(default=None, ge=0)
    output: str = ""
    error: str = ""
