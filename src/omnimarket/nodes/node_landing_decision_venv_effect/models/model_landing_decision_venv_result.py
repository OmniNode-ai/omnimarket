# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one decision environment pass."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLandingDecisionVenvResult(BaseModel):
    """``built`` and ``verified`` carry the interpreter and the versions read back from it;
    ``refused`` carries every library whose installed version differs from the pin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["built", "verified", "refused"]
    pin_commit: str = ""
    python: str = ""
    installed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        return (
            f"landing_decision_venv={self.status} pin={self.pin_commit[:8]} "
            f"installed={len(self.installed)} reasons={len(self.reasons)}"
        )
