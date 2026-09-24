# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class every check declares: gate or range (unified plan section 2b)."""

from __future__ import annotations

from enum import StrEnum


class EnumCheckClass(StrEnum):
    """How a check decides. Both classes block; the class never makes a check advisory.

    A gate judges a deterministic subject from one observation, and a red means
    a defect every time. A range judges a genuinely sampled subject (model
    output) as a distribution against a floor over n runs, and a red means its
    acceptance line was missed. A flaky measurement of a deterministic subject
    is a broken gate, never a range.
    """

    GATE = "gate"
    RANGE = "range"
