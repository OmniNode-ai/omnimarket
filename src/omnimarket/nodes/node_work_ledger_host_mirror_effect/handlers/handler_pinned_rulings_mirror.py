# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPinnedRulingsMirror -- the rulings a brief cites stay on a host's mirror whatever their age (OMN-26412).

A lab host's mirror holds the authority rows of the last day and the rulings of the last 30 days, because the
bus projection answers for recent rows. A brief cites its landing ruling by stamp and lane, and the lane-brief
reader looks for that row in the file it is given and in the ``archive/`` directory beside it. The standing
landing ruling is older than 30 days, so a brief built on a lab host stopped at ``lane_brief.py constraints``
exit 3 (read on h202, 2026-11-22: "no row reads '2026-09-29T06:34:17Z | RULING | lane=orchestrator-83 |'").

This handler copies each declared ruling, verbatim, from the host's own archive rolls (the registry clone tracks
them; the projection holds no row that old) into one roll beside the mirror, ``archive/pinned-rulings-split.md``,
which the brief reader already globs. It rewrites the roll only when its content changes, in stamp order, so
two runs write the same bytes. A ruling found in no roll is named, not skipped: the caller reports it.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_pinned_rulings_mirror import (
    EnumPinnedRulingsStatus,
    ModelPinnedRulingsRequest,
    ModelPinnedRulingsResult,
)


class HandlerPinnedRulingsMirror:
    def handle(self, request: ModelPinnedRulingsRequest) -> ModelPinnedRulingsResult:
        rolls = (
            sorted(request.rolls_directory.glob("*-split.md"))
            if request.rolls_directory.is_dir()
            else []
        )
        found: dict[str, str] = {}
        for roll in rolls:
            with roll.open(encoding="utf-8", errors="replace") as held:
                for line in held:
                    for pin in request.pinned:
                        if pin.prefix not in found and line.startswith(pin.prefix):
                            found[pin.prefix] = line.rstrip("\n")
        missing = tuple(p.prefix for p in request.pinned if p.prefix not in found)
        text = "".join(f"{found[key]}\n" for key in sorted(found))
        target = request.target_path
        if text or target.exists():
            self._write_if_changed(target, text)
        return ModelPinnedRulingsResult(
            status=EnumPinnedRulingsStatus.MISSING
            if missing
            else EnumPinnedRulingsStatus.MIRRORED,
            rows_written=len(found),
            target_path=str(target),
            missing=missing,
            detail=(
                f"no archive roll in {request.rolls_directory} holds: {'; '.join(missing)}"
                if missing
                else ""
            ),
        )

    @staticmethod
    def _write_if_changed(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            return
        held = target.with_suffix(target.suffix + ".tmp")
        held.write_text(text, encoding="utf-8")
        held.replace(target)
