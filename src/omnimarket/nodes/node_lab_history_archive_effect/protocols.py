# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the lab history archive: the uploader and the command runner (OMN-17427)."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .models import ModelLabHistoryArchivePartEvent


class ModelDriveObject(BaseModel):
    """What Drive reports for one stored object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    size: int
    md5: str


class DriveUploadError(RuntimeError):
    """The uploader could not store or read back an object."""


class ProtocolCommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]: ...


class ProtocolPartEventSink(Protocol):
    def emit(self, event: ModelLabHistoryArchivePartEvent) -> None: ...


class ProtocolDriveUploader(Protocol):
    def check_ready(self) -> tuple[str, str] | None:
        """None when ready, else (refusal code name, detail). Reads no credential value."""
        ...

    def upload(self, local: Path, remote_dir: str, bytes_per_second: int) -> None: ...

    def remote_object(self, remote_dir: str, name: str) -> ModelDriveObject | None: ...
