# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One request to resolve a captured secret reference (OMN-20926)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCapturedSecretResolveRequest(BaseModel):
    """A reference found in a captured record, and who is asking for its value.

    ``accessor`` names the caller (a node, lane or service) for the access log.
    It is not an authorization claim: the store authorizes, against the
    identity the caller built it with.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1, max_length=512)
    accessor: str = Field(min_length=1, max_length=200, pattern=r"\S")


__all__ = ["ModelCapturedSecretResolveRequest"]
