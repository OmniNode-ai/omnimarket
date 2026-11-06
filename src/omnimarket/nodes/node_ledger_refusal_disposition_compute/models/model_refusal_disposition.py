# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A recorded disposition is a proposal until all its independent evidence validates."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
RequestId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]
Reason = Literal["duplicate_delivery", "superseded", "operator_abandoned"]


class ModelRefusalDisposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: Literal["ledger-refusal-disposition/v1"]
    artifact_name: str
    request_id: RequestId
    refusal_sha256: Sha256
    reason: Reason
    approval_sha256: Sha256
    evidence_sha256: tuple[Sha256, ...] = Field(min_length=1)
