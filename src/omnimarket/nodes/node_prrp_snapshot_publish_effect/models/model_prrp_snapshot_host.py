# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A deployment host supplied by the caller of the snapshot publisher."""

from pydantic import BaseModel, ConfigDict


class ModelPrrpSnapshotHost(BaseModel):
    """The host's display name, ssh destination and deployment availability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    target: str
    local: bool = False
    available: bool = True
    unavailable_reason: str = ""
