# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Header-read GitHub quota (OMN-19826).

Quota comes only from the ``x-ratelimit-*`` headers of a response the effect
already received. The ``rate_limit`` endpoint misreports the shared bucket and
is never read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HEADER_LIMIT = "x-ratelimit-limit"
_HEADER_REMAINING = "x-ratelimit-remaining"
_HEADER_USED = "x-ratelimit-used"
_HEADER_RESET = "x-ratelimit-reset"
_HEADER_RESOURCE = "x-ratelimit-resource"

# Token shapes GitHub issues, plus an Authorization value. The identity is a
# label for the credential (its contract ref name), never the credential.
_TOKEN_SHAPE = re.compile(
    r"(?:\bgh[opsur]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\b(?:bearer|token)\s)",
    re.IGNORECASE,
)


class ModelGithubQuotaHeadersMissingError(ValueError):
    """A response lacked an x-ratelimit-* header; the reading is refused, not defaulted."""


class ModelGithubQuotaReading(BaseModel):
    """One GitHub rate-limit reading, taken from response headers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = Field(ge=0)
    remaining: int = Field(ge=0)
    used: int = Field(ge=0)
    reset: int = Field(ge=0, description="Epoch seconds when the window resets.")
    resource: str = Field(min_length=1, description="core, graphql, search, ...")
    identity: str = Field(
        min_length=1,
        description="Label of the credential that spent this quota (the contract "
        "secret ref name). Never a token value.",
    )

    @field_validator("identity")
    @classmethod
    def _identity_is_not_a_token(cls, value: str) -> str:
        if _TOKEN_SHAPE.search(value):
            raise ValueError("identity must be a credential label, not a token value")
        return value

    @classmethod
    def from_response_headers(
        cls, headers: Mapping[str, str], *, identity: str
    ) -> ModelGithubQuotaReading:
        """Build a reading from a response's headers (case-insensitive names).

        Raises:
            ModelGithubQuotaHeadersMissingError: when any x-ratelimit-* header
                is absent. A missing header is never defaulted.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        missing = [
            name
            for name in (
                _HEADER_LIMIT,
                _HEADER_REMAINING,
                _HEADER_USED,
                _HEADER_RESET,
                _HEADER_RESOURCE,
            )
            if name not in lowered
        ]
        if missing:
            raise ModelGithubQuotaHeadersMissingError(
                f"response lacks rate-limit header(s): {', '.join(missing)}"
            )
        return cls(
            limit=int(lowered[_HEADER_LIMIT]),
            remaining=int(lowered[_HEADER_REMAINING]),
            used=int(lowered[_HEADER_USED]),
            reset=int(lowered[_HEADER_RESET]),
            resource=lowered[_HEADER_RESOURCE],
            identity=identity,
        )
