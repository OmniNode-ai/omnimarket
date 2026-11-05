# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded process-health payloads for the floor and the detailed report."""

from pydantic import BaseModel, ConfigDict, Field


class ModelProcessHealthPayloadPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_source_bytes: int = Field(gt=0)
    max_summary_bytes: int = Field(gt=0)
    detail_file: str = Field(pattern=r"^[a-z0-9-]+\.json\.gz$")


class ModelProcessHealthPayloadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str


class ModelProcessHealthPayloadResult(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    summary: bytes | None = None
    detail: bytes | None = None
    detail_file: str = ""
    reason: str = ""
