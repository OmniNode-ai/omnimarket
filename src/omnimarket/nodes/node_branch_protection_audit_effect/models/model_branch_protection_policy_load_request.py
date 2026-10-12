# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the audit policy from a deployment overlay."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelBranchProtectionPolicyLoadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_path: Path | None = None
    # When true, a missing overlay path or file is an error, never the empty
    # default fleet.
    overlay_required: bool = False


__all__ = ["ModelBranchProtectionPolicyLoadRequest"]
