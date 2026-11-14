# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate store contract suite file is frozen with a hash; a changed suite fails the check."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

SUITE_DIR = Path(__file__).resolve().parents[1] / "nodes" / "node_gate_store_effect"
SUITE = SUITE_DIR / "contract_suite.py"
FROZEN = SUITE_DIR / "contract_suite.sha256"


def check_suite_is_frozen(suite_bytes: bytes, frozen: str) -> None:
    actual = hashlib.sha256(suite_bytes).hexdigest()
    assert actual == frozen.strip(), (
        f"contract suite is {actual}; frozen {frozen.strip()}"
    )


def test_suite_file_matches_its_frozen_hash() -> None:
    check_suite_is_frozen(SUITE.read_bytes(), FROZEN.read_text(encoding="utf-8"))


def test_a_changed_suite_fails_the_check() -> None:
    """Positive control: one appended byte, or a weakened assertion, no longer matches."""
    frozen = FROZEN.read_text(encoding="utf-8")
    original = SUITE.read_bytes()
    with pytest.raises(AssertionError, match="frozen"):
        check_suite_is_frozen(original + b"\n", frozen)
    weakened = original.replace(b"assert all(", b"assert any(", 1)
    assert weakened != original
    with pytest.raises(AssertionError, match="frozen"):
        check_suite_is_frozen(weakened, frozen)
