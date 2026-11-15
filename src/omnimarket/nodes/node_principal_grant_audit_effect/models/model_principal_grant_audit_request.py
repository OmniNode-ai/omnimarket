# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the principal grant audit handler (OMN-21622)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelPrincipalGrantAuditRequest:
    """One principal grant audit pass.

    registry_root is the registry the ledger path resolves against; dry_run builds and
    returns the argv and runs nothing.
    """

    registry_root: Path
    dry_run: bool = False
