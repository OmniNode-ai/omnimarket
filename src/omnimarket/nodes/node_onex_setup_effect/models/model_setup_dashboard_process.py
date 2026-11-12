# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Owned dashboard process metadata; the session token stays in the secret store."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSetupDashboardProcess(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    pid: int = Field(gt=1)
    process_stamp: str = Field(min_length=1)
    url: str = Field(pattern=r"^http://127\.0\.0\.1:[0-9]{1,5}$")
    credential_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,200}$")
    tenant_id: str = Field(min_length=1)
