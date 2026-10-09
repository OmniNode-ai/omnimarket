# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Audited reasons for retiring an acceptance-criterion proposal (OMN-20157)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumAcBindingRetirementReason(StrEnum):
    """Why a proposed binding no longer claims a criterion."""

    SUPERSEDED_BY = "superseded_by"
    NO_LONGER_APPLICABLE = "no_longer_applicable"
