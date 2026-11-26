# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An item of contracts/OMN-26705.yaml scoped local_done_gate binds no criterion.

The hosted dod-verify run does not evaluate a local_done_gate item, so a criterion bound only through one stays
unproven there. The same rule is held for contracts/OMN-25342.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26705.yaml"


def _entries() -> list[dict[str, Any]]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return list(document["dod_evidence"])


def test_omn26705_local_done_gate_items_carry_no_binds_ac() -> None:
    local = [e for e in _entries() if e.get("execution_scope") == "local_done_gate"]
    assert {e["id"] for e in local} == {
        "dod-omn26705-integration-proof"
    }  # positive control: the scan found the lab item
    assert [e["id"] for e in local if e.get("binds_ac")] == []


def test_omn26705_every_criterion_is_bound_by_a_hosted_item() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    criteria = {a["id"] for r in document["requirements"] for a in r["acceptance"]}
    hosted = {
        ac
        for e in _entries()
        if e.get("execution_scope") != "local_done_gate"
        for ac in e.get("binds_ac", [])
    }
    assert criteria == {"AC1", "AC2", "AC3", "AC4"}
    assert criteria <= hosted
