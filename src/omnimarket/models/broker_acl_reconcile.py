# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the dev-broker ACL reconcile."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelBrokerAclReconcileRequest(BaseModel):
    """dry_run and apply act on every declared and derived row. ready_check asks one node's question (OMN-24005): does
    the broker already hold every row the node's principals derive for it. It reads only and never creates a row."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["dry_run", "apply", "ready_check"]
    node: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _node_only_for_ready_check(self) -> ModelBrokerAclReconcileRequest:
        if (self.mode == "ready_check") != (self.node is not None):
            raise ValueError("node is given for mode ready_check and for no other mode")
        return self


class ModelBrokerAclReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["dry_run", "apply", "ready_check"]
    exit_code: int
    declared: int | None = Field(default=None, ge=0)
    live: int | None = Field(default=None, ge=0)
    missing: int | None = Field(default=None, ge=0)
    undeclared: int | None = Field(default=None, ge=0)
    missing_rows: tuple[str, ...] = ()
    # Rows derived from contract topics for the declared principals, and topics that still have no row (None: not derived).
    derived: int | None = Field(default=None, ge=0)
    uncovered_topics: int | None = Field(default=None, ge=0)
    # ready_check only: True when the broker holds every row derived for the node, False when it lacks one (missing_rows
    # names each), None when the answer could not be read. A node that cannot read its answer does not start as ready.
    ready: bool | None = None
    output: str = ""
    error: str = ""
