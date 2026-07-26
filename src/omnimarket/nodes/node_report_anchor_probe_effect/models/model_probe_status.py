# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed status vocabulary for report content-anchor probes (OMN-15164)."""

from __future__ import annotations

from enum import StrEnum


class EnumAnchorProbeStatus(StrEnum):
    """Outcome of a single anchor probe (sha, path, or PR-number claim).

    Shared across all three claim kinds so a consumer (the OMN-15163 COMPUTE
    validator) can branch on one closed vocabulary instead of three
    partially-overlapping ones. ``detail`` on the owning probe-result model
    carries the free-text specifics (e.g. "resolves to a blob, not a commit").
    """

    RESOLVED = "resolved"
    NOT_RESOLVED = "not_resolved"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    ESCAPES_ROOT = "escapes_root"
    MISSING_CONTEXT = "missing_context"
    LOOKUP_FAILED = "lookup_failed"


__all__ = ["EnumAnchorProbeStatus"]
