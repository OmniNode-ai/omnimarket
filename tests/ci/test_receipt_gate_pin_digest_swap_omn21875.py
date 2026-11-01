# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21875: the receipt-gate caller pins a tag that has the digest-swap fix.

omnibase_core#2214 (merge commit 2fc5e461f, first dev-merge tag
dev-20261101103701-2fc5e461fca7) lets a writer-app pin bump through as
pin-only when its work-contract YAML changes only refresh the uv.lock
digest. The caller must pin that tag or a later one; older pins refuse the
bot core-pin-bump PRs with "pin swap: path is not allowed".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.ci.receipt_gate_pin import RECEIPT_GATE_PIN_TAG, RECEIPT_GATE_REUSABLE

pytestmark = pytest.mark.unit

CALLER = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)
FIX_TAG_STAMP = 20261101103701


def _pinned_tag() -> str:
    doc = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
    uses: str = doc["jobs"]["repo-evidence"]["uses"]
    reusable, _, tag = uses.partition("@")
    assert reusable == RECEIPT_GATE_REUSABLE
    return tag


def test_caller_pin_is_at_or_after_the_digest_swap_fix_tag() -> None:
    tag = _pinned_tag()
    match = re.fullmatch(r"dev-(\d{14})-[0-9a-f]{12}", tag)
    assert match, f"{tag!r} is not a dev-merge tag"
    assert int(match.group(1)) >= FIX_TAG_STAMP, (
        f"{tag} predates dev-20261101103701-2fc5e461fca7 (omnibase_core#2214)"
    )


def test_test_side_pin_constant_moves_with_the_caller() -> None:
    assert _pinned_tag() == RECEIPT_GATE_PIN_TAG
