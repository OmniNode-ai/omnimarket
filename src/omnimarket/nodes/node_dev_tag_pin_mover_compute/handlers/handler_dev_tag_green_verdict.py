# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a sibling's dev merge commit is green, judged on dev's required checks only.

The dev-tag train moves a pin to the newest dev tag whose commit is green.
Judging every check run let a red that gates no merge hold a pin back: on
2026-11-05 the Auto-Merge workflow, which exits 1 on purpose when it refuses a
feature-base pull request, kept every omnimarket dev tag red. Only the base
branch's required status checks count now.

A dev merge commit carries only the required checks that run on push; the
pull-request-only ones never appear on it. So the verdict judges the required
checks that ran: red if any is red, unread if one is still running or none ran,
and red when the required list itself is unreadable or empty, so an unreadable
branch never turns every tag green. Pure: the reads live in ``train_sources``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_green_verdict import (
    EnumDevTagGreenVerdict,
    ModelDevTagGreenVerdictRequest,
    ModelDevTagGreenVerdictResult,
)

GREEN_CONCLUSIONS: Final = frozenset({"success", "skipped", "neutral"})
_VERDICTS: Final = {
    True: EnumDevTagGreenVerdict.GREEN,
    False: EnumDevTagGreenVerdict.RED,
    None: EnumDevTagGreenVerdict.UNREAD,
}


def required_checks_verdict(
    runs: Iterable[tuple[str, str, str]], required: frozenset[str] | None
) -> bool | None:
    """Green, red or None (unread) from (name, status, conclusion) runs of one commit."""
    if not required:
        return False
    kept = [(status, done) for name, status, done in runs if name in required]
    if not kept:
        return None
    if any(done and done not in GREEN_CONCLUSIONS for _, done in kept):
        return False
    if any(status != "completed" for status, _ in kept):
        return None
    return True


class HandlerDevTagGreenVerdict:
    """COMPUTE handler: one dev merge commit's verdict on dev's required checks."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelDevTagGreenVerdictRequest
    ) -> ModelDevTagGreenVerdictResult:
        runs = [(r.name, r.status, r.conclusion) for r in request.runs]
        verdict = required_checks_verdict(runs, request.required_contexts)
        return ModelDevTagGreenVerdictResult(verdict=_VERDICTS[verdict])


__all__ = ["GREEN_CONCLUSIONS", "HandlerDevTagGreenVerdict", "required_checks_verdict"]
