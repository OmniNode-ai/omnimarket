# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every setting of the pilot manifest check. The defaults are the in-memory profile."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPilotManifestCheckOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hash_algorithm: Literal["sha256"] = "sha256"
    require_frozen_before_start: bool = True
    missing_metric_fails_bar: bool = True
