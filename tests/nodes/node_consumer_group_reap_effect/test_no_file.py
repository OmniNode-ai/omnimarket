# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reap effect decides from the command and reads or writes no file (OMN-23992, RULING 2026-11-12T15:16:12Z)."""

from __future__ import annotations

import pytest

from tests.nodes.node_consumer_group_reap_effect.support import REAP_DIR

pytestmark = pytest.mark.unit

FORBIDDEN = ("empty_since.json", "write_text(", "read_text(", "open(", "Path(")


def test_no_source_file_touches_the_file_system() -> None:
    offenders = []
    for path in REAP_DIR.rglob("*.py"):
        text = path.read_text()
        offenders += [f"{path.name}: {token}" for token in FORBIDDEN if token in text]
    assert offenders == []
