# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the changed keys of a declared data file come from (OMN-26275)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelDataFileChange,
)


class ProtocolSourceFileDiff(Protocol):
    def changes(
        self, repo_root: str, base_ref: str, head_ref: str, paths: Sequence[str]
    ) -> tuple[ModelDataFileChange, ...]:
        """For each path, the TOML keys that differ between its text at ``base_ref`` and ``head_ref``.

        A path with an absent side or a side that is not TOML comes back with ``parse_failed`` set.
        """
        ...
