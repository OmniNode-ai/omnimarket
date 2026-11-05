# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host-local lab lane retention pass."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .model_file_retention import ModelFileRetentionEntry


class ModelLabLaneRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    entry: ModelFileRetentionEntry
    now: dt.datetime
    dry_run: bool = False
