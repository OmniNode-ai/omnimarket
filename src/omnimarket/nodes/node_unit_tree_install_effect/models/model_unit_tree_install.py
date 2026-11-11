# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of node_unit_tree_install_effect."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelUnitTreeInstallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["install", "verify", "dry-run"]
    unit: str = Field(min_length=1)
    host: str = Field(min_length=1)
    #: Empty means use the unit row's own ref.
    ref: str = ""


class ModelUnitTreeCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ok: bool
    detail: str = ""


class ModelUnitTreeInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    unit: str
    #: One of pass | refused | rolled-back | failed | fail | dry-run.
    verdict: str
    install_path: str = ""
    commit: str = ""
    checks: tuple[ModelUnitTreeCheck, ...] = ()
    #: The previous tree kept as a rollback, when one existed.
    kept: str = ""
    #: The previous tree restored after a rollback, when one existed.
    restored: str = ""
    detail: str = ""
