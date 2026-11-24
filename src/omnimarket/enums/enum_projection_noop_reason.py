# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why a projection writer returned zero rows on purpose (OMN-26410).

The runtime logs an ERROR for a delivery that wrote zero rows unless the writer
attests, under ``NOOP_REASON_KEY``, one of these reasons. Restated rather than
imported from the runtime, as ``ROWS_REFUSED_KEY`` is: the writer modules are
loaded by processes that do not import the runtime's wiring package. The runtime
honours ``tick`` only for a delivery on the runtime tick topic, and a writer
attests ``skipped`` only for a delivery it read and found nothing in for it.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Final

#: The result key the runtime reads for an attested zero-row no-op.
NOOP_REASON_KEY: Final[str] = "projection_noop_reason"


@unique
class EnumProjectionNoopReason(StrEnum):
    """The closed set of reasons a writer may attest for a designed zero-row return."""

    TICK = "tick"
    SKIPPED = "skipped"
