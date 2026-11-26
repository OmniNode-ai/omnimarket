# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result, event and overlay of node_ledger_rows_loader_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    ModelReaderResult,
)


class EnumRowsSource(StrEnum):
    """Where the lines came from: the bus-fed node, the file (logged), or nowhere."""

    NODE = "node"
    FILE_FALLBACK = "file-fallback"
    NONE = "none"


class EnumRowsStatus(StrEnum):
    """ok: a source answered. unreadable: no source answered; never an empty ok."""

    OK = "ok"
    UNREADABLE = "unreadable"


class ModelRowsLoaderRequest(BaseModel):
    """One named reader, its window (the last N hours, or a since/until history range) and the reader's rows answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    window_hours: int | None = Field(default=None, ge=1, le=720)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    node_answer: ModelReaderResult

    @model_validator(mode="after")
    def _one_window(self) -> ModelRowsLoaderRequest:
        if (self.window_hours is None) == (self.since is None):
            raise ValueError("a read names exactly one of window_hours or since")
        if self.until is not None and self.since is None:
            raise ValueError("until needs since")
        if (
            self.since is not None
            and self.until is not None
            and self.until < self.since
        ):
            raise ValueError("until is before since")
        return self


class ModelRowsLoaderEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReaderEventKind
    topic: str
    reader: str
    source: EnumRowsSource
    status: EnumRowsStatus
    reason: str
    at: AwareDatetime
    row_count: int
    fallback_count: int


class ModelRowsLoaderResult(BaseModel):
    """The answer a caller prints; unreadable never carries lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str
    status: EnumRowsStatus
    source: EnumRowsSource
    reason: str
    lines: tuple[str, ...] = ()
    row_count: int
    fallback_count: int
    event: ModelRowsLoaderEvent | None = None

    def log_line(self) -> str:
        """One machine-readable line; a file answer carries ``source=file-fallback``."""
        return (
            f"ROWS-LOADER reader={self.reader} status={self.status.value} "
            f"source={self.source.value} reason={self.reason} rows={self.row_count} "
            f"fallback_count={self.fallback_count}"
        )
