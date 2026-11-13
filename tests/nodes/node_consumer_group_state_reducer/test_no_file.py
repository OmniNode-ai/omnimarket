# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer holds group state in its record and reads or writes no file (OMN-23992, RULING 2026-11-12T15:16:12Z)."""

from __future__ import annotations

import pytest

from tests.nodes.node_consumer_group_state_reducer.support import NODE_DIR

pytestmark = pytest.mark.unit

FORBIDDEN = ("empty_since.json", "write_text(", "read_text(", "open(", "Path(")
# The overlay module resolves its own example file through the shared overlay reader, which this test does not scan.
EXEMPT = {"overlay.py"}


def test_no_source_file_touches_the_file_system() -> None:
    offenders = []
    for path in NODE_DIR.rglob("*.py"):
        if path.name in EXEMPT:
            continue
        text = path.read_text()
        offenders += [f"{path.name}: {token}" for token in FORBIDDEN if token in text]
    assert offenders == []
