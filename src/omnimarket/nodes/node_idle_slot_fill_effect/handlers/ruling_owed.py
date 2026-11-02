# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's owed line for each mechanism ticket a lane left unfinished and unclaimed."""

from __future__ import annotations

from omnimarket.models.model_ruling_mechanism_owed_request import (
    ModelRulingMechanismOwedRequest,
)
from omnimarket.nodes.node_ruling_mechanism_owed_compute.handlers import (
    HandlerRulingMechanismOwed,
)

from .ruling_live_checks import Finding


def owed_findings(rows: list[dict[str, str]], now: str) -> list[Finding]:
    """One MISSING finding per owed row; the pure owed compute decides, this only words it."""
    owed = HandlerRulingMechanismOwed().handle(
        ModelRulingMechanismOwedRequest(
            rows=tuple(r["text"] for r in rows), tick_at=now
        )
    )
    return [
        (
            "missing",
            row.key,
            f"mechanism ticket {row.ticket} owed: lane={row.lane} ended {row.outcome} "
            f"at {row.terminal_at} with no CLAIM after one tick; "
            f"blocker={row.blocker or 'not recorded'}; rulings={','.join(row.rulings)}",
            f"dispatch a lane on {row.ticket} from the terminal's staged work; "
            "a CLAIM on the ticket clears this line",
        )
        for row in owed.owed
    ]
