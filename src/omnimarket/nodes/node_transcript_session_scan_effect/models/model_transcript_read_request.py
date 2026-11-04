# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the in-process transcript read."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_classification import ModelSourceRevision


class ModelTranscriptReadRequest(BaseModel):
    """One manifest entry to read and the revision the scan recorded for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_name: str
    relative_path: str
    scanned_source_revision: ModelSourceRevision | None = None
