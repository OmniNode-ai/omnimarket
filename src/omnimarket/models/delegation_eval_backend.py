# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit lab eval pin; independent of the serving call identity producer."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDelegationEvalBackend(BaseModel):
    """One endpoint and its expected, attested model artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    host: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_file: str = Field(min_length=1)
    quant: str = Field(min_length=1)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("endpoint must be an HTTP origin without credentials")
        return value.rstrip("/")
