# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pairwise Jaccard agreement of repeat call sets, and matched removal of unstable cells."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def min_pairwise(repeats: Sequence[Sequence[str]]) -> float | None:
    if len(repeats) < 2:
        return None
    return min(jaccard(a, b) for a, b in combinations(repeats, 2))
