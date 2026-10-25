# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the TLC result summary node."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelTlcResultSummaryRequest(BaseModel):
    """The run directory whose results/*.out files are summarized."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    run_dir: Path


class ModelTlcResultSummaryResult(BaseModel):
    """One summary line per results/*.out file, in file name order."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[str, ...]
