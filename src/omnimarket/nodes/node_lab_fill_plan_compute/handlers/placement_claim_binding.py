# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The placement CLAIM is written under the sidecar lane name, the name the ticket-creation gate binds on (OMN-26216).

The gate resolves the creating session's lane from the harness sidecar and refuses a create unless that lane
holds an open CLAIM (`reason=issue-create-not-bound-to-a-commitment`, `lane_source=sidecar`). On
2026-11-20 a placement lane whose sidecar name was `place-evalsovl-1225` wrote its CLAIM as `lane=evalsovl`;
the CLAIM existed, under another name, so every create it made was refused and the refusals sat as FRICTION
rows nobody read. Both functions are pure: the sidecar name goes in, the same name comes out in the CLAIM's
`lane=` cell, and the binding test reads that cell whole, never a prefix or a stem of it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from omnimarket.handlers.ledger_row_grammar import parse_row

ACTOR = "script:onex-remote-lane"
_TERMINAL_CLOSES = "TERMINAL"


def _cell(name: str, value: str) -> str:
    if not value or any(ch in value for ch in "|\n\r"):
        raise ValueError(f"{name} must be one non-empty ledger cell, got {value!r}")
    return value


def placement_claim_row(
    *,
    at: datetime,
    sidecar_lane: str,
    ticket: str,
    host: str,
    run: str,
    parent: str,
    cost_hours: float,
    displaces: str,
    scope: str,
) -> str:
    """The CLAIM a placement writes: `lane=` is the sidecar name, and `host=` and `run=` make it a placement.

    `host=` and `run=` are what the throughput tick reads as placement (a TERMINAL is not one).
    """
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | CLAIM | lane={_cell('sidecar_lane', sidecar_lane)} | ticket={_cell('ticket', ticket)}"
        f" | actor={ACTOR} | model=none"
        f" | est ~{cost_hours:g} lane-hours; displaces {_cell('displaces', displaces)}; ({ticket})"
        f" | worktree=none | host={_cell('host', host)} | run={_cell('run', run)}"
        f" | parent={_cell('parent', parent)} | {_cell('scope', scope)}"
    )


def commitment_bound(rows: Iterable[str], sidecar_lane: str) -> bool:
    """Whether the gate finds a commitment for `sidecar_lane`: an open CLAIM whose `lane=` is that whole name.

    Open means no TERMINAL of the same lane after the CLAIM. A CLAIM under any other name, a stem of it
    included, binds nothing.
    """
    open_claim = False
    for line in rows:
        parsed = parse_row(line, types=frozenset({"CLAIM", _TERMINAL_CLOSES}))
        if parsed is None or parsed.value("lane") != sidecar_lane:
            continue
        open_claim = parsed.row_type == "CLAIM"
    return open_claim


__all__ = ["ACTOR", "commitment_bound", "placement_claim_row"]
