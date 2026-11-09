# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract suite is frozen with a hash; a changed suite fails the check."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SUITE_DIR = Path(__file__).parents[1] / "nodes" / "node_gate_store_effect"
SUITE = SUITE_DIR / "contract_suite.py"
FROZEN = SUITE_DIR / "contract_suite.sha256"


def matches_frozen(suite_bytes: bytes) -> bool:
    return (
        hashlib.sha256(suite_bytes).hexdigest()
        == FROZEN.read_text(encoding="utf-8").strip()
    )


def test_suite_file_matches_its_frozen_hash() -> None:
    assert matches_frozen(SUITE.read_bytes()), "contract_suite.py changed after freeze"


def test_a_changed_suite_fails_the_check() -> None:
    changed = SUITE.read_bytes() + b"\n# a check weakened after the freeze\n"
    assert not matches_frozen(changed)
