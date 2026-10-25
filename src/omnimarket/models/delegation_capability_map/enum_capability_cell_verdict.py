# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The range-line verdict a capability cell reads (OMN-20169)."""

from enum import StrEnum


class EnumCapabilityCellVerdict(StrEnum):
    """MET clears the class line at the required n, MISSED does not, REFUSED is under-powered."""

    MET = "MET"
    MISSED = "MISSED"
    REFUSED = "REFUSED"
