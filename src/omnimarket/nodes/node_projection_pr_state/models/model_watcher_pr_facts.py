# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only the watcher facts parity needs; PR bodies are never retained."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelWatcherPrFacts(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    repo: str
    number: int
    state: Literal["OPEN", "CLOSED", "MERGED"]
    head_sha: str
    draft: bool
    armed: bool
    labels: tuple[str, ...]
