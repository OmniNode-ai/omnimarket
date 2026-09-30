# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fields compared by parity; other export columns are deliberately ignored."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPrStateParityRow(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    repo: str
    pr_number: int
    state: Literal["open", "closed", "merged"]
    head_sha: str
    draft: bool
    armed: bool
    labels: tuple[str, ...]
    watcher_class: str
    ci_verdict: Literal["GREEN", "RED", "PENDING", "NONE"]

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.pr_number}"
