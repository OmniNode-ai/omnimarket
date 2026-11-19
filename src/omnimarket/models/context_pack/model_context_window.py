# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared-window check of a context profile, kept beside the profile so the profile file stays small."""

from __future__ import annotations

from typing import Protocol


class _ProfileWindow(Protocol):
    """The window fields the check reads; the profile model satisfies it without being imported here."""

    @property
    def token_budget(self) -> int: ...

    @property
    def context_window_tokens(self) -> int | None: ...

    @property
    def reserved_output_tokens(self) -> int | None: ...

    @property
    def reserved_system_tokens(self) -> int | None: ...

    def effective_window_tokens(self) -> int | None: ...


def check_context_window(profile: _ProfileWindow) -> None:
    """Refuse a window that is declared in part, or whose effective size is below the profile's token budget."""
    window = (
        profile.context_window_tokens,
        profile.reserved_output_tokens,
        profile.reserved_system_tokens,
    )
    if all(value is None for value in window):
        return
    if any(value is None for value in window):
        raise ValueError(
            "context_window_tokens, reserved_output_tokens and "
            "reserved_system_tokens are declared together: a window is its "
            "declared size minus its reserved output and system shares"
        )
    effective = profile.effective_window_tokens()
    if effective is None or effective < profile.token_budget:
        raise ValueError(
            f"effective window {effective} (context_window_tokens minus the "
            f"reserved output and system shares) is below token_budget "
            f"{profile.token_budget}"
        )


__all__ = ["check_context_window"]
