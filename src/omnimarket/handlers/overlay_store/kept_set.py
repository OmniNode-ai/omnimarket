# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay documents a host's refresh may keep a copy of: never one whose readers have moved to the central store.

``CUT_OVER_OVERLAYS`` grows when a group's readers have moved (the floor fill's cutover pull request names its overlay
here), and from then on a refresh whose kept set holds one refuses, naming it, and writes no snapshot. No overlay is added to
a kept set after its cutover.
"""

from __future__ import annotations

from collections.abc import Iterable

CUT_OVER_OVERLAYS: frozenset[str] = frozenset()


class KeptSetCutOverError(ValueError):
    """A host's refresh was asked to keep a copy of an overlay the central store serves."""


def require_no_cut_over_overlay(kept: Iterable[str]) -> None:
    held = sorted(set(kept) & CUT_OVER_OVERLAYS)
    if held:
        raise KeptSetCutOverError(
            f"the central store serves {', '.join(held)}: a host refresh keeps no copy of it"
        )


__all__ = ["CUT_OVER_OVERLAYS", "KeptSetCutOverError", "require_no_cut_over_overlay"]
