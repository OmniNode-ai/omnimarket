# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24651: the contract carries each acceptance criterion and names where it is met."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24651.yaml"


def _contract() -> dict[str, Any]:
    data = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _criteria() -> dict[str, dict[str, Any]]:
    return {
        a["id"]: a for req in _contract()["requirements"] for a in req["acceptance"]
    }


def test_the_contract_carries_ac1_ac2_and_ac3() -> None:
    assert set(_criteria()) == {"AC1", "AC2", "AC3"}


def test_ac1_and_ac3_name_where_they_are_met() -> None:
    criteria = _criteria()
    assert "2026-11-16T01:00:28Z" in criteria["AC1"]["statement"]
    assert "frx-delegroute-0005" in criteria["AC1"]["statement"]
    assert "Lab: head=" in criteria["AC3"]["statement"]
    assert "delegated=" in criteria["AC3"]["statement"]


def test_every_criterion_is_bound_by_a_dod_evidence_entry_with_a_check() -> None:
    bound: dict[str, list[dict[str, Any]]] = {}
    for entry in _contract()["dod_evidence"]:
        for ac in entry.get("binds_ac", []):
            bound.setdefault(ac, []).extend(entry["checks"])
    for ac in _criteria():
        assert bound.get(ac), f"{ac} has no dod_evidence check"


def test_the_summary_names_every_code_that_holds() -> None:
    summary = _contract()["summary"]
    for code in ("1308", "1310", "1316 to 1321"):
        assert code in summary, f"the summary does not name {code}"
