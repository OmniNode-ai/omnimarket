# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge the dev consumer-group lag samples that close OMN-23196 AC2.

AC2 reads: on dev, lag samples are non-zero in fewer than half the samples over
two hours with the alarm's bound unchanged. The samples come from the lab alarm
sampler as a list of ``{at, nonzero_groups}``. This is the one rule both the
hosted proof and the local Done gate judge them by.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def lag_samples_meet_bar(
    samples: Sequence[Mapping[str, object]],
    *,
    min_samples: int,
    max_nonzero_share: float,
) -> bool:
    """True when enough samples exist and fewer than ``max_nonzero_share`` read non-zero.

    A sample reads non-zero when its ``nonzero_groups`` is above zero. Too few
    samples is never a pass, and a sample that does not carry a whole-number
    ``nonzero_groups`` is refused naming its position, so a malformed sampler
    file cannot read as a good one.
    """
    nonzero = 0
    for position, sample in enumerate(samples):
        groups = sample.get("nonzero_groups")
        if not isinstance(groups, int) or isinstance(groups, bool) or groups < 0:
            raise ValueError(
                f"sample {position} has no whole-number nonzero_groups: {groups!r}"
            )
        nonzero += groups > 0
    return len(samples) >= min_samples and nonzero < len(samples) * max_nonzero_share
