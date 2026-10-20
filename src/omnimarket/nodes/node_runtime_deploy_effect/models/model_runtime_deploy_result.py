# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment terminal outcome, including the preserved host command streams."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelRuntimeDeployResult(BaseModel):
    """A nonzero host preflight or deployment outcome is a terminal failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    repository_root: Path
    returncode: int
    stdout: str
    stderr: str
    locus: str = "host-process"
