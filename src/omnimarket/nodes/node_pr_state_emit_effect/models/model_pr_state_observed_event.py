# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable PR observation with a content-derived delivery identity (OMN-19999).

Canonical JSON uses sorted keys, compact separators, UTF-8 (unescaped Unicode),
no NaN, and the declared tuple order. Only observed_at and digest are excluded.
A supplied digest is checked, so corrupted wire payloads cannot claim an identity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_state_emit_effect.models.model_pr_state_emit_request import (
    ModelPrStateEmitRequest,
)


class ModelPrStateObservedEvent(ModelPrStateEmitRequest):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"observed_at", "digest"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.digest and self.digest != digest:
            raise ValueError("digest does not match canonical PR state")
        object.__setattr__(self, "digest", digest)
        return self
