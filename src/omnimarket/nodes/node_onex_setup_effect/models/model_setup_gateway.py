# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit native connection selection and owned gateway process evidence."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_setup_dashboard_process import ModelSetupDashboardProcess


class ModelSetupGatewaySelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model: str = Field(min_length=1)
    port: int = Field(default=0, ge=0, le=65535)

    @field_validator("model")
    @classmethod
    def single_line(cls, value: str) -> str:
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Select a nonempty single-line model ID")
        return value


class ModelSetupGatewayProcess(ModelSetupDashboardProcess):
    selection: ModelSetupGatewaySelection
    backend_id: str = Field(min_length=1)
    connection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proved: bool = False
    proof_run_id: UUID | None = None
