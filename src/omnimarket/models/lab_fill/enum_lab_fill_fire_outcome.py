# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed vocabularies of the lab-fill fire outcome judge.

Every value here is a word the fire's receipt uses. A channel says how the
judge read the fire's own written result file; an alert category says which
alert the fire owes. The alert category values are the category words the
delivery record carries; the judge reads none of them as settings.
"""

from __future__ import annotations

from enum import StrEnum


class EnumLabFillResultChannel(StrEnum):
    """How the judge read the fire's own written result file.

    ``read``: the file existed, parsed and validated. ``absent``: no file.
    ``invalid``: a file existed but failed to parse or to validate. ``None``
    (not a member) is the channel of a selftest fire, which is not judged.
    """

    READ = "read"
    ABSENT = "absent"
    INVALID = "invalid"


class EnumLabFillAlertCategory(StrEnum):
    """Which alert the fire owes, as the delivery record names it."""

    REFUSED = "lab_fill_refused"
    NO_RESULT = "lab_fill_no_result"
    IDLE = "lab_fill_idle"
    BLIND = "lab_fill_blind"


__all__: list[str] = [
    "EnumLabFillAlertCategory",
    "EnumLabFillResultChannel",
]
