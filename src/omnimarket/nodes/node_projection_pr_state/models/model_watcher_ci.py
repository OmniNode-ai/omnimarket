# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Watcher's nullable CI observation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelWatcherCi(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    verdict: Literal["GREEN", "RED", "PENDING", "NONE"]
