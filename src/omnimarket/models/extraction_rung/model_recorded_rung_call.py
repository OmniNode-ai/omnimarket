# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One recorded delegation call to a rung (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRecordedRungCall(BaseModel):
    """The exact response bytes of a call, base64, with the digests that pin them.

    request_digest is the sha256 hex of the prompt bytes sent; response_digest the
    sha256 hex of the response bytes. served_model_id is the model id the call's
    attribution named. expected_quotes are the quotes the case must propose, used to
    count misses; empty when the case has no ground truth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1)
    request_digest: str = Field(min_length=64, max_length=64)
    response_b64: str
    response_digest: str = Field(min_length=64, max_length=64)
    served_model_id: str = Field(min_length=1)
    expected_quotes: tuple[str, ...] = ()
