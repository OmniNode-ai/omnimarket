# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of building one receipt."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLandingTlcPublishResult(BaseModel):
    """``published`` carries the release tag and the archive; ``refused`` carries the reasons."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["published", "refused"]
    pin: str = ""
    tag: str = ""
    archive: Path | None = None
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        if self.status == "published":
            return (
                f"landing_tlc_receipt=published tag={self.tag} archive={self.archive}"
            )
        return f"landing_tlc_receipt={self.status} reasons={len(self.reasons)}"
