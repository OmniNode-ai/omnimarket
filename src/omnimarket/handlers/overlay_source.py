# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A deployment overlay read on first use, held for the handler that asks (OMN-21536).

An orchestrator handler is built inside the effects runtime, where a constructor
that reads the deployment and raises takes every other node's runtime down with
it. The overlay is therefore read when a run asks for it, and kept, so the run's
phases share one read. The kept overlay is state, so it lives here, in a
collaborator the handler is given, and not on the handler: a public handler
member never assigns to the handler.
"""

from __future__ import annotations

from collections.abc import Callable

__all__: list[str] = ["OverlaySource"]


class OverlaySource[OverlayT]:
    """One overlay, injected or loaded on first ``get`` and then kept.

    ``loader`` raises when the overlay is absent or invalid, so a run that cannot
    read its overlay is refused at the first ``get``, never defaulted.
    """

    def __init__(
        self, loader: Callable[[], OverlayT], overlay: OverlayT | None = None
    ) -> None:
        self._loader = loader
        self._overlay = overlay

    def get(self) -> OverlayT:
        """The overlay; read through the loader on the first call when none was injected."""
        if self._overlay is None:
            self._overlay = self._loader()
        return self._overlay
