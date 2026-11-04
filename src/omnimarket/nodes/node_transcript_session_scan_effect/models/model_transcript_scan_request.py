# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the transcript scan."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelTranscriptScanRequest(BaseModel):
    """Which overlay roots to scan; an empty tuple scans every root the overlay names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_names: tuple[str, ...] = ()
