# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One detected secret to store (OMN-20926)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelCapturedSecretStoreRequest(BaseModel):
    """A secret the content scrubber found, and where it was captured.

    ``value`` is a ``SecretStr``: a dumped or logged request prints the mask.
    ``session_id`` and ``captured_at`` become the stored secret's comment;
    nothing else about the surrounding content is ever passed in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: SecretStr
    session_id: str = Field(min_length=1, max_length=200)
    captured_at: datetime


__all__ = ["ModelCapturedSecretStoreRequest"]
