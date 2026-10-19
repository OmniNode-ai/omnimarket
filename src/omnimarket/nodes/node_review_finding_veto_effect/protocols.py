# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of node_review_finding_veto_effect."""

from __future__ import annotations

from typing import Protocol


class ProtocolHeadFileReader(Protocol):
    """Reads one file as it is at a commit; None when it is absent or not text."""

    def read(self, head_sha: str, path: str) -> str | None: ...


class ProtocolDiffReader(Protocol):
    """Reads which lines of each file a commit range adds or changes."""

    def changed_lines(
        self, old_sha: str, new_sha: str, *, from_merge_base: bool
    ) -> dict[str, tuple[tuple[int, int], ...]] | None:
        """New-side inclusive line ranges per path; None when the range cannot be diffed.

        ``from_merge_base`` diffs from the merge base of the two commits, as a pull
        request shows it; otherwise from ``old_sha`` itself.
        """
        ...


__all__: list[str] = ["ProtocolDiffReader", "ProtocolHeadFileReader"]
