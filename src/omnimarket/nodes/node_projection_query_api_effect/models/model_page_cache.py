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
        description=(
            "How long a held page is fresh; a read after this serves the held page as stale "
            "and refreshes it behind the response, until stale_seconds more have passed."
        ),
    )
    stale_seconds: int = Field(
        ge=1,
        description=(
            "How long past ttl_seconds a held page may still be served while it is refreshed; "
            "a read after ttl_seconds + stale_seconds finds nothing and recomputes the page."
        ),
    )
    refresh_lock_seconds: int = Field(
        ge=1,
        description=(
            "How long one runtime's claim to refresh a page lasts in the shared Valkey, so one "
            "refresh per page runs across every runtime; it ends sooner when the refresh ends."
        ),
    )
    key_prefix: str = Field(min_length=1)
    valkey: ModelPageCacheValkey


__all__ = ["ModelPageCacheSettings", "ModelPageCacheValkey"]
