# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The page cache this node keeps its projection pages in, as the node contract declares it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPageCacheValkey(BaseModel):
    """Where the Valkey is: the names of the environment variables that hold its address and password."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_env: str = Field(min_length=1)
    port_env: str = Field(min_length=1)
    password_env: str | None = Field(default=None, min_length=1)
    database: int = Field(ge=0)
    connect_timeout_seconds: int = Field(ge=1)
    operation_timeout_seconds: int = Field(ge=1)


class ModelPageCacheSettings(BaseModel):
    """The contract's ``page_cache`` block: how long a page is held and where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ttl_seconds: int = Field(
        ge=1,
        description="How long a served page is held; a read after this misses and recomputes.",
    )
    key_prefix: str = Field(min_length=1)
    valkey: ModelPageCacheValkey


__all__ = ["ModelPageCacheSettings", "ModelPageCacheValkey"]
