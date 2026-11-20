# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code graph tests read no host's overlay roots: the resolver's inputs are the test's own."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_overlay_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No registry root, no code graph clones root and an empty home, so the resolver finds only what a test sets."""
    for name in (
        "OMNIBASE_PATH",
        "OMNI_HOME",
        "ONEX_SKILL_OVERLAY_ROOTS",
        "ARCH_GRAPH_BOLT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
