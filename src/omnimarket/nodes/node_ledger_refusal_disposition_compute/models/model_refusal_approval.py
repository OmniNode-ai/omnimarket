# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An operator grant binds one exact refusal, reason and bounded validity."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model_refusal_disposition import Reason, RequestId, Sha256


class ModelRefusalApproval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: Literal["ledger-refusal-approval/v1"]
    artifact_name: str
    request_id: RequestId
    refusal_sha256: Sha256
    reason: Reason
    valid_until: str
