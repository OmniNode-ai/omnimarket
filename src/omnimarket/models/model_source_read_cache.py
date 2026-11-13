# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the source read cache effect."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)


class EnumSourceCacheOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    STORED = "stored"
    BYPASS = "bypass"
    UNAVAILABLE = "unavailable"


class ModelSourceCacheKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    params_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)

    def render(self, prefix: str) -> str:
        return f"{prefix}{self.source_id}:{self.params_digest}:{self.revision}"

    @classmethod
    def digest(cls, params: Mapping[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ModelSourceCacheRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["lookup", "store"]
    key: ModelSourceCacheKey
    payload: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_payload(self) -> ModelSourceCacheRequest:
        if self.operation == "store" and self.payload is None:
            raise ValueError("a store needs a payload")
        if self.operation == "lookup" and self.payload is not None:
            raise ValueError("a lookup carries no payload")
        return self


class ModelSourceCacheResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSourceCacheOutcome
    payload: str | None = None
    reason: str = ""


class ModelSourceCacheTally(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: int = 0
    misses: int = 0
    stores: int = 0
    bypassed: int = 0
    unavailable: int = 0
    source_reads: int = 0
    source_seconds: float = 0.0
    backend: str = ""
    disabled_reason: str = ""

    def as_json_line(self) -> str:
        return json.dumps({"source_cache": self.model_dump()}, sort_keys=True)


class ModelSourceCacheValkey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host_env: str = Field(min_length=1)
    port_env: str = Field(min_length=1)
    password_env: str | None = Field(min_length=1)
    # The NAME of the environment variable that holds a secret store reference (secret://onex/<folder>/<KEY>) for the password; used when the variable named by password_env holds none; absent, only password_env is read.
    password_ref_env: str | None = Field(default=None, min_length=1)
    database: int = Field(ge=0)
    connect_timeout_seconds: int = Field(ge=1)
    operation_timeout_seconds: int = Field(ge=1)


class ModelSourceCacheOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_prefix: str = Field(min_length=1)
    ttl_backstop_seconds: int = Field(ge=1)
    # Where a repository's canonical clone is, relative to the registry home; ``{name}`` is the repository's name after the owner. The
    # default branch's tip there is the revision of a source that can change only by a commit (a workflow file's existence).
    clone_relpath_template: str = Field(min_length=1)
    clone_revision_timeout_seconds: int = Field(ge=1)
    # The store: a `valkey` block names the Valkey this deployment keeps its answers in; with no block the answers are kept in memory for
    # the run (the default, which a test or a host with no store uses).
    valkey: ModelSourceCacheValkey | None = None
    # The store the password reference is resolved through (addressing and the identity's references, never a value); absent, the reader falls back to the machine's own configuration.
    secret_store: ModelSecretStoreOverlay | None = None
