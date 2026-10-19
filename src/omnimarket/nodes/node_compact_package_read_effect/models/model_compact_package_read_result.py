# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One result from node_compact_package_read_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCompactPackageReadResult(BaseModel):
    """The package, or an empty one with the reason none was found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    found: bool
    package: str
    detail: str


__all__ = ["ModelCompactPackageReadResult"]
