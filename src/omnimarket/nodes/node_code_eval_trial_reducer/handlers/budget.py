# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Budget counting of a try sequence (rule 7), pure.

Budget k reads the first k verdicts of a sequence: a pass when one is ``pass``;
else, when any is ``unscorable``, the sequence leaves n at that budget and gives
no observation; else a non-pass. A budget's denominator counts the sequences it
keeps.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.models.enum_code_score import (
    EnumCodeScoreVerdict,
)


def budget_reading(verdicts: Sequence[EnumCodeScoreVerdict], k: int) -> bool | None:
    """True a pass, False a non-pass, None when the sequence leaves n at this budget."""
    head = verdicts[:k]
    if EnumCodeScoreVerdict.PASS in head:
        return True
    if EnumCodeScoreVerdict.UNSCORABLE in head:
        return None
    return False


def budget_counts(
    sequences: Iterable[Sequence[EnumCodeScoreVerdict]], budgets: Sequence[int]
) -> dict[int, tuple[int, int]]:
    """Per budget, (n, passes) over the sequences the budget keeps."""
    kept = [list(seq) for seq in sequences]
    counts: dict[int, tuple[int, int]] = {}
    for k in budgets:
        readings = [
            r for r in (budget_reading(seq, k) for seq in kept) if r is not None
        ]
        counts[k] = (len(readings), sum(1 for r in readings if r))
    return counts


__all__ = ["budget_counts", "budget_reading"]
