# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serving facts declared by routing authority, independent of a model alias."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelBackendIdentity(BaseModel):
    """One physical serving configuration; opaque provider facts stay explicit.

    A managed provider may declare ``provider-undisclosed`` for model_file and
    quant. That declaration does not assert a measured local quantization.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    model_file: str = Field(min_length=1)
    quant: str = Field(min_length=1)
    context_tokens: int = Field(gt=0, strict=True)

    @field_validator("host", "model_file", "quant")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("serving identity fields must be nonblank and trimmed")
        return value

    @property
    def fingerprint(self) -> str:
        """Stable score partition including host, weights, quant and context."""
        encoded = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def validate_backend_identities(backends: Sequence[Mapping[str, Any]]) -> None:
    """Reject contradictory declarations before either routing path selects.

    Legacy declarations remain readable and are flagged unrecorded on attempts.
    Disabled endpoints do not participate in alias ambiguity checks.
    """
    aliases: dict[str, tuple[str, str]] = {}
    for backend in backends:
        declared = backend.get("serving_identity")
        endpoint = backend.get("endpoint_url")
        if declared is None or not endpoint:
            continue
        identity = ModelBackendIdentity.model_validate(declared)
        if urlsplit(str(endpoint)).hostname != identity.host:
            raise ValueError(f"identity_host_mismatch: {backend.get('backend_id')}")
        alias = str(backend.get("model_name") or "")
        weights = (identity.model_file, identity.quant)
        if alias in aliases and aliases[alias] != weights:
            raise ValueError(
                f"ambiguous_model_alias: {alias}; split aliases by model file and quant"
            )
        aliases[alias] = weights
