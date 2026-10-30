# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19450 AC1: the contract carries the amended reading, not the literal text.

No truncation cause exists, so five distinct causes cannot hold. The contract's
AC1 must say the five faults are told apart by cause or by the deciding
attempt's truncated flag, and must not claim five distinct causes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-19450.yaml"


def _ac1_statements() -> list[str]:
    contract: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return [
        str(ac["statement"])
        for req in contract["requirements"]
        for ac in req.get("acceptance", [])
        if ac["id"] == "AC1"
    ]


def test_ac1_names_the_truncated_flag_and_the_amendment() -> None:
    statements = _ac1_statements()
    assert statements
    for statement in statements:
        assert "truncated flag" in statement
        assert "amended 2026-09-25" in statement


def test_ac1_does_not_claim_five_distinct_causes() -> None:
    for statement in _ac1_statements():
        assert "ends in a terminal with a distinct cause" not in statement
