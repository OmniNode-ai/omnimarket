# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""IncrementalScanState -- durable state model for the incremental corpus scanner.

[OMN-11845]
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProcessedFileRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    sha256: str
    processed_at: datetime


class IncrementalScanState(BaseModel):
    model_config = ConfigDict(frozen=True)

    last_run_timestamp: datetime
    processed_files: tuple[ProcessedFileRecord, ...]
    scan_repos: tuple[str, ...]
    total_files_scanned: int
    total_files_published: int


__all__: list[str] = ["IncrementalScanState", "ProcessedFileRecord"]
