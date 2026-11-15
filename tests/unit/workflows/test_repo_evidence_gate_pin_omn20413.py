# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repo-evidence caller pins an omnibase_core receipt-gate that retries the PGDG key fetch (OMN-20413).

The receipt-gate's "Provide the PostgreSQL 16 server tools" step fetches the
PGDG signing key with curl. Plain --retry skips curl error 55 (SSL EOF), so one
dropped connection failed repo-evidence / dod-verify and, through the
Repo Evidence Dependency job, the whole CI run. omnibase_core fixed this in
dev-20261113222915-b853619df394; the caller must not pin an older tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "call-repo-evidence-gate.yml"
FIXED_TAG_STAMP = "20261113222915"
pytestmark = pytest.mark.unit


def test_receipt_gate_pin_carries_the_pgdg_retry_fix() -> None:
    text = WORKFLOW.read_text()
    match = re.search(
        r"omnibase_core/\.github/workflows/receipt-gate\.yml@dev-(\d{14})-[0-9a-f]{12}",
        text,
    )
    assert match, "receipt-gate pin not found"
    assert match.group(1) >= FIXED_TAG_STAMP
