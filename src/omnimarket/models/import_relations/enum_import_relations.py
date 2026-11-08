# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums of the import relation report."""

from __future__ import annotations

from enum import StrEnum


class EnumRelationFindingKind(StrEnum):
    """Why a tracked relation reads as a finding."""

    #: An endpoint label has no owning piece in the overlay.
    UNMAPPED_ENDPOINT = "unmapped-endpoint"
    #: An endpoint's owning piece carries no join in the overlay.
    PIECE_WITHOUT_JOIN = "piece-without-join"
    #: The graph holds no occurrence of the tracked relation.
    NOT_IN_GRAPH = "not-in-graph"
