# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket a ledger RULING row carries, so a brief cites the ruling's own ticket, not the run's parent ticket.

A ledger row is ``<ts> | <TYPE> | key=value | ...``. The ticket of a ruling is the ``ticket=`` cell of its own RULING row,
never the run's parent ticket. The facts come in as arguments; nothing here reads a file, a clone, the network or the clock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_TICKET = re.compile(r"OMN-[0-9]+")


def ruling_row_ticket(lines: Iterable[str], stamp: str, lane: str) -> str:
    """The single ``OMN-<n>`` in the ``ticket=`` cell of the RULING row whose first cell is ``stamp`` and whose ``lane=`` cell is ``lane``.

    The lane match is case-insensitive. A missing ruling, a row with no ``ticket=`` cell, or more than one matching row with
    differing tickets is refused with a ``ValueError`` naming the stamp and lane; a ruling is never defaulted to the parent ticket.
    """
    tickets: list[str] = []
    for line in lines:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3 or cells[1] != "RULING" or cells[0] != stamp:
            continue
        values = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
        if values.get("lane", "").lower() != lane.lower():
            continue
        ticket = _TICKET.fullmatch(values.get("ticket", ""))
        if ticket is None:
            raise ValueError(
                f"no ticket cell on the RULING row for stamp {stamp!r} lane {lane!r}"
            )
        tickets.append(ticket.group(0))
    if not tickets:
        raise ValueError(f"no RULING row for stamp {stamp!r} lane {lane!r}")
    if len(set(tickets)) > 1:
        raise ValueError(
            f"conflicting tickets {sorted(set(tickets))} on the RULING rows for stamp {stamp!r} lane {lane!r}"
        )
    return tickets[0]
