# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and document of the host table keys ingest effect (OMN-23983)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from omnimarket.nodes.node_host_table_compute.models.model_host_table import (
    ModelHostTableResult,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
NoSpaceStr = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
OverlayIdStr = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class ModelHostTableKeysOverlay(_Frozen):
    """The overlay document of the ingest node: every value comes from the overlay."""

    overlay_id: OverlayIdStr
    key_prefix: NoSpaceStr
    valkey_host_env: NonEmptyStr
    valkey_port_env: NonEmptyStr
    valkey_password_env: NonEmptyStr
    connect_timeout_seconds: int = Field(gt=0)
    stale_after_seconds: int = Field(gt=0)


class ModelHostTableKeysDocument(_Frozen):
    """What is stored at the one key: the host table document."""

    digest: str
    source_ref: str
    loaded_at: datetime
    ingested_at: datetime
    table: ModelHostTableResult

    @model_validator(mode="after")
    def _never_an_empty_table(self) -> ModelHostTableKeysDocument:
        if not self.table.rows:
            raise ValueError("the host table document holds no rows")
        return self


class ModelHostTableKeysIngestResult(_Frozen):
    """The outcome of one ingest."""

    outcome: Literal["written", "ignored"]
    key: str
    digest: str
    hosts: int
    admitted: int


class ModelHostTableKeysReading(_Frozen):
    """The state of a read of the host table keys."""

    state: Literal["READ", "UNKNOWN"]
    reason: str
    document: ModelHostTableKeysDocument | None

    @model_validator(mode="after")
    def _state_and_reason_fit(self) -> ModelHostTableKeysReading:
        if self.state == "READ":
            if self.document is None:
                raise ValueError("a READ needs a document")
            if self.reason:
                raise ValueError("a READ has no reason")
        else:
            if not self.reason:
                raise ValueError("an UNKNOWN needs a reason")
            if self.document is not None:
                raise ValueError("an UNKNOWN holds no document")
        return self
