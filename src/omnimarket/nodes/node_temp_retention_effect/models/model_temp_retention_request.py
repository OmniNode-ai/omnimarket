# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request of one retention pass."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelTempRetentionRequest(BaseModel):
    """Run one pass for ``host_name``.

    ``overlay_file`` pins the overlay document; when empty the node reads
    ``ONEX_TEMP_RETENTION_OVERLAY`` and then ``<root>/node_temp_retention_effect/overlay.yaml``
    under ``ONEX_SKILL_OVERLAY_ROOTS``. ``dry_run`` reads and reports and removes nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    overlay_file: str = ""
    dry_run: bool = False
