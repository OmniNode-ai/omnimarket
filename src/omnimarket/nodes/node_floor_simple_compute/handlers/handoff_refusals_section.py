# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's handed-off refusals section from the cycle's reading (OMN-23242). Pure.

Counts, over the pull requests a lane handed to the landing controller, those the controller defers or keeps out now,
and those deferred more than ``silent_after_ticks`` ticks with no parent MSG on the ledger for their head and gate.
A reading that was not taken draws the section unread with the reason, never as zero.
"""

from __future__ import annotations

from omnimarket.models.floor_17a.model_floor_handoff_refusals import (
    ModelHandoffRefusalLine,
    ModelHandoffRefusalsIn,
    ModelHandoffRefusalsOverlay,
    ModelHandoffRefusalsSection,
)

NOT_READ = "this cycle did not read the controller's refusals"


def build_handoff_refusals_section(
    overlay: ModelHandoffRefusalsOverlay, reading: ModelHandoffRefusalsIn | None
) -> ModelHandoffRefusalsSection:
    """The section: unread with the reason, or read with the counts and the refusals that were counted."""
    if reading is None:
        return ModelHandoffRefusalsSection(
            status="unread", title=overlay.title, reason=NOT_READ
        )
    if reading.status == "unread" or reading.ledger is None:
        return ModelHandoffRefusalsSection(
            status="unread",
            title=overlay.title,
            reason=reading.reason or NOT_READ,
        )
    handed = {row.pr: row for row in reading.ledger.handed}
    told = {(row.pr, row.head, row.gate) for row in reading.ledger.told}
    lines = tuple(
        ModelHandoffRefusalLine(
            pr=r.pr,
            head=r.head,
            gate=r.gate,
            kind=r.kind,
            ticks=r.ticks,
            handoff_lane=handed[r.pr].lane,
            parent_told=(r.pr, r.head, r.gate) in told,
        )
        for r in sorted(reading.refusals, key=lambda r: (r.pr, r.gate, r.head))
        if r.pr in handed and r.gate not in overlay.queue_gates
    )
    deferred = {line.pr for line in lines if line.kind == "deferred"}
    kept_out = {line.pr for line in lines if line.kind == "kept_out"}
    silent = sorted(
        {
            line.pr
            for line in lines
            if line.kind == "deferred"
            and line.ticks > overlay.silent_after_ticks
            and not line.parent_told
        }
    )
    refused = len(deferred | kept_out)
    text = (
        f"{refused} handed-off pull request{'' if refused == 1 else 's'} deferred or kept out; "
        f"{len(silent)} deferred more than {overlay.silent_after_ticks} ticks with no parent MSG"
    )
    return ModelHandoffRefusalsSection(
        status="read",
        title=overlay.title,
        refused=refused,
        deferred=len(deferred),
        kept_out=len(kept_out),
        silent=len(silent),
        silent_after_ticks=overlay.silent_after_ticks,
        silent_prs=tuple(silent[: overlay.max_listed]),
        lines=lines[: overlay.max_listed],
        text=text,
    )


__all__ = ["NOT_READ", "build_handoff_refusals_section"]
