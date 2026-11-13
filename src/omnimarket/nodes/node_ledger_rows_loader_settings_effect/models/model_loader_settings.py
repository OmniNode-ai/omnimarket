# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result, event and hosts overlay of node_ledger_rows_loader_settings_effect."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from omnimarket.models.model_work_ledger_reader import EnumReaderEventKind

NonEmptyStr = Annotated[str, Field(min_length=1)]


class EnumLoaderSettingsStatus(StrEnum):
    """resolved: the host's row was found. host-row-missing: no row for this host. refused: an overlay is missing or malformed."""

    RESOLVED = "resolved"
    HOST_ROW_MISSING = "host-row-missing"
    REFUSED = "refused"


class ModelLoaderHostRow(BaseModel):
    """One host's loader command; `{registry_root}` in a part is the host's registry root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    command: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ModelLoaderHostsOverlay(BaseModel):
    """The ledger_rows_loader_hosts overlay: every deployment fact, no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["ledger_rows_loader_hosts.v1"]
    timeout_seconds: StrictInt = Field(ge=1, le=600)
    hosts: tuple[ModelLoaderHostRow, ...]

    @field_validator("hosts", mode="before")
    @classmethod
    def _no_rows_is_empty(cls, value: Any) -> Any:
        return () if value is None else value

    @model_validator(mode="after")
    def _one_row_per_host(self) -> ModelLoaderHostsOverlay:
        names = [row.host.lower() for row in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("two rows name the same host")
        return self


class ModelLoaderSettingsRequest(BaseModel):
    """This host's registry root and its own short native name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_root: str = Field(min_length=1)
    host: str = Field(min_length=1)


class ModelLoaderSettingsEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReaderEventKind
    topic: str
    status: EnumLoaderSettingsStatus
    reason: str
    host: str
    at: AwareDatetime


class ModelLoaderSettingsResult(BaseModel):
    """What a caller needs to run the loader: the argv, its bound and the overlay's fallback flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumLoaderSettingsStatus
    reason: str
    file_fallback_allowed: bool | None
    argv: tuple[str, ...] | None
    timeout_s: int | None
    host: str
    event: ModelLoaderSettingsEvent

    def payload(self) -> dict[str, Any]:
        """The answer without its event, as the CLI prints it."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "file_fallback_allowed": self.file_fallback_allowed,
            "argv": None if self.argv is None else list(self.argv),
            "timeout_s": self.timeout_s,
            "host": self.host,
        }
