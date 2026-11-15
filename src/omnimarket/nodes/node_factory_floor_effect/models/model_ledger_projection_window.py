# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger window the work ledger projection read produced, and what it could not say (OMN-24490).

``rows`` is the window's rows, reduced per lane in day order with today last. ``unread_days`` are the UTC days
(``YYYY-MM-DD``) whose rows never entered the window because the read deadline was spent on other days, and
``gaps`` is one sentence per reading that returned fewer rows than it matched or that did not read at exact
parity, so the window's cause names the days and the answers it left out.
"""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow


class ModelProjectionWindow(BaseModel):
    """The window the projection read produced: its rows, the days the deadline was spent on, and its gaps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelFloorLedgerRow, ...]
    # The UTC days (``YYYY-MM-DD``) not read because the read deadline was spent on other days.
    unread_days: tuple[str, ...] = ()
    # One sentence per reading that returned fewer rows than it matched, or that did not read at exact parity.
    gaps: tuple[str, ...] = ()

    def cause(self) -> str:
        """Why the window is incomplete: the days the deadline was spent on, then each partial answer, ``; ``-joined; empty when it is complete."""
        parts: list[str] = []
        if self.unread_days:
            parts.append(
                f"work ledger projection days unread, the read deadline was spent: {', '.join(self.unread_days)}"
            )
        parts.extend(self.gaps)
        return "; ".join(parts)
