# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one installer pass."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLandingTlcSyncResult(BaseModel):
    """``synced`` installed a receipt, ``current`` found nothing to do, ``refused`` kept a release
    whose receipt failed the gate out of the state directory, ``failed`` could not read them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["synced", "current", "refused", "failed"]
    installed: tuple[str, ...] = ()
    current: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        return (
            f"landing_tlc_sync={self.status} installed={len(self.installed)} "
            f"current={len(self.current)} reasons={len(self.reasons)}"
        )
