# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the brief gate says about one brief."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneBriefPrGateVerdict(StrEnum):
    """``reported`` is a finding that does not stop the brief; ``refused`` stops it."""

    ADMITTED = "admitted"
    ADMITTED_OVERRIDE = "admitted_override"
    REPORTED = "reported"
    REFUSED = "refused"
