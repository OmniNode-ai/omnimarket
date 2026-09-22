# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether one definition-of-done verification counts as done for the eval metric.

Two members, not three. A third "unknown" member would be a place for a caller
to put a verdict it could not resolve, and the whole point of the refusal enum
beside this one is that every non-done outcome names WHY. An unresolvable input
is a refusal with a reason, never an absence.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodEvalOutcome(StrEnum):
    """Terminal outcome of the eval metric's done predicate."""

    # The verification passed AND proved at least one behaviour. This is the
    # only value the metric in section 3.1 of the Jev typed-decision shadow
    # plan counts as "definition-of-done verifies true".
    DONE = "done"

    # It did not. The paired refusal member says which conjunct failed.
    REFUSED = "refused"


__all__ = ["EnumDodEvalOutcome"]
