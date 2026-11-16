# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcomes a lab proof judge use can be recorded with."""

from __future__ import annotations

from enum import StrEnum


class EnumUseOutcome(StrEnum):
    """not_applicable: the declared class does not apply to the change.

    refused_no_substitute: the change qualifies but has no substitute proof.
    """

    not_applicable = "not_applicable"
    refused_no_substitute = "refused_no_substitute"


__all__ = ["EnumUseOutcome"]
