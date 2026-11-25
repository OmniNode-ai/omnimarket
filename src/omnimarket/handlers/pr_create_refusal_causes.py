# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The causes of an ``onex scm pr-create`` refusal, named from a STATUS or TERMINAL row's own words (OMN-26216).

Shared by the refusal alert compute node and the failure alarm router, so the two never name a cause two ways.
"""

import re
from dataclasses import dataclass

KINDS = frozenset({"STATUS", "TERMINAL"})


@dataclass(frozen=True)
class Cause:
    name: str
    pattern: re.Pattern[str]
    says: str


# Order matters: the first cause whose pattern matches names the row's cause, specific words before the general ones.
CAUSES: tuple[Cause, ...] = (
    Cause(
        "reader-exit-69",
        re.compile(
            r"exit(?:ed)? 69\b|bus[- ]session[- ]failed[- ]timeouterror|bus session TimeoutError",
            re.I,
        ),
        "the ledger reader answered the duplicate guard with exit 69 (bus session timeout), so pr-create refused unchecked",
    ),
    Cause(
        "reader-timed-out",
        re.compile(r"reader \S+ timed out after \d+(?:\.\d+)?s"),
        "the guard's wait for the ledger reader ended, so pr-create refused unchecked",
    ),
    Cause(
        "reader-overlay-not-configured",
        re.compile(
            r"overlay (?:file )?(?:is )?not configured|NodeOverlayNotFoundError"
        ),
        "the ledger reader found no overlay on the host, so pr-create refused unchecked",
    ),
    Cause(
        "reader-not-moved",
        re.compile(r"reader-not-in-moved-readers"),
        "the ledger reader's overlay does not name the guard's reader as moved, so pr-create refused unchecked",
    ),
    Cause(
        "pr-create-timeout",
        re.compile(r"pr-create\b[^\n]{0,120}?\b(?:hung|timed out)\b|\brc 124\b"),
        "onex scm pr-create ran until the lane's own wait ended and opened nothing",
    ),
    Cause(
        "guard-unchecked",
        re.compile(r"PR-DUPLICATE-GUARD unchecked"),
        "the duplicate guard could not read a source, so pr-create refused unchecked",
    ),
)
REFUSAL = re.compile(
    r"PR-DUPLICATE-GUARD unchecked|pr-create\b[^\n]{0,120}?\b(?:hung|timed out)\b|\brc 124\b"
)
