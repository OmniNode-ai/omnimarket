# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bounded legacy state directory to inspect, never execute."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelSetupLegacyImportRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    directory: Path
