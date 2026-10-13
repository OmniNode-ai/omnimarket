# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prerequisite findings and the exact commit that was inspected."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelTicketContractPrerequisiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    head_sha: str | None = None
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures
