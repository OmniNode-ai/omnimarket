# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure aggregation helpers for the user-correction signal (OMN-12846).

The context-selection-quality signal counts ONLY ``MISUNDERSTANDING``-axis
corrections (context was present but failed to convey intent). ``NEW_INFORMATION``
corrections are recorded on the event but excluded from the context-failure rate
(they describe a requirement that did not exist at injection time, not a context
failure).

Anti-sycophancy invariant: this module feeds context selection ONLY. It must
never import or reference any agent-output scoring, reward, ranking, or RLHF
surface. The guard test ``test_correction_rate_not_wired_to_agent_output_reward``
enforces this by inspecting this module's imports.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.intelligence.events import ModelUserCorrectionEvent


def context_selection_failure_count(
    events: Iterable[ModelUserCorrectionEvent],
) -> int:
    """Count corrections that count against context selection.

    Only ``MISUNDERSTANDING``-axis corrections are tallied; ``NEW_INFORMATION``
    corrections are excluded.
    """
    return sum(1 for event in events if event.counts_toward_context_failure)


def context_selection_failure_rate(
    events: Iterable[ModelUserCorrectionEvent],
) -> float:
    """Fraction of corrections attributable to context-selection failure.

    The denominator is the total number of corrections (both axes); the
    numerator is the ``MISUNDERSTANDING`` count. Returns ``0.0`` for an empty
    set (no corrections observed implies no measured failure).
    """
    materialized = list(events)
    if not materialized:
        return 0.0
    return context_selection_failure_count(materialized) / len(materialized)


__all__ = [
    "context_selection_failure_count",
    "context_selection_failure_rate",
]
