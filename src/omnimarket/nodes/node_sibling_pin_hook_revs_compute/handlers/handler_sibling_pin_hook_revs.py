# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which tag the hook blocks and which tag the source pin take on a train night (OMN-22641).

Two tests hold every hook block of the pin's repository to a dev merge tag not older than the
locked pin, and first-party-ref allows one home per tag literal. So a hook block that follows the
pin must sit strictly NEWER than it: equal is refused as two homes, older fails the floor test.
The train used to move the pin to the newest green tag and leave the hooks behind
(omniclaude#3188, 2026-11-06). The plan puts the newest green tag on the floor-bound hook blocks
and the newest green tag strictly under the lowest of them on the pin. Hook blocks pinned older
than the pin are not read against it and stay.

Pure: the effect reads the tags and their CI and writes the files.
"""

from __future__ import annotations

import re

from ..models import (
    EnumHookRevsReason,
    ModelSiblingPinHookRevsPlan,
    ModelSiblingPinHookRevsRequest,
)


class HandlerSiblingPinHookRevs:
    """Plan the hook rev and pin moves; never write."""

    def handle(
        self, request: ModelSiblingPinHookRevsRequest
    ) -> ModelSiblingPinHookRevsPlan:
        pattern = re.compile(request.tag_pattern)

        def stamp(tag: str) -> str | None:
            match = pattern.fullmatch(tag)
            return match.group("stamp") if match else None

        current = stamp(request.pin_tag) or ""
        newer = []
        for candidate in request.candidates:
            candidate_stamp = stamp(candidate.tag) or ""
            if candidate_stamp <= current:
                break
            newer.append((candidate.tag, candidate_stamp, candidate.green is True))

        newest = next(((t, s) for t, s, green in newer if green), None)
        if newest is None:
            return _plan(None, (), None, EnumHookRevsReason.NO_NEWER_GREEN_TAG)
        target_tag, target = newest

        bound = [
            (index, rev_stamp)
            for index, rev in enumerate(request.hook_revs)
            if (rev_stamp := stamp(rev)) is not None and rev_stamp >= current
        ]
        if not bound:
            return _plan(
                None, (), target_tag, EnumHookRevsReason.PIN_MOVED_NO_BOUND_HOOKS
            )

        ceiling = min(max(rev_stamp, target) for _, rev_stamp in bound)
        under = next((t for t, s, green in newer if green and s < ceiling), None)
        movers = tuple(index for index, rev_stamp in bound if rev_stamp < target)
        hook_tag = target_tag if movers else None
        if under is None:
            return _plan(
                hook_tag, movers, None, EnumHookRevsReason.NO_GREEN_TAG_UNDER_HOOKS
            )
        reason = (
            EnumHookRevsReason.HOOKS_AND_PIN_MOVED
            if movers
            else EnumHookRevsReason.PIN_MOVED_HOOKS_ALREADY_AHEAD
        )
        return _plan(hook_tag, movers, under, reason)


def _plan(
    hook_tag: str | None,
    hook_indexes: tuple[int, ...],
    pin_tag: str | None,
    reason: EnumHookRevsReason,
) -> ModelSiblingPinHookRevsPlan:
    return ModelSiblingPinHookRevsPlan(
        hook_tag=hook_tag, hook_indexes=hook_indexes, pin_tag=pin_tag, reason=reason
    )
