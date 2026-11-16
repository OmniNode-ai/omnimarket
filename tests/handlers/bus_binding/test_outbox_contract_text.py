# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox reuse contracts say what their evidence shows at the merge base.

AC1 of the bound-tests contract wants a non-zero exit at the merge base, so its falsifier is an exit of zero
there, under the one selection its evidence command runs. AC2 of the reuse contract is a preservation
criterion: its behavioural tests also pass at the merge base, which sent every replay.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SELECTION = re.compile(r'-k "([^"]+)"')


def _load(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((CONTRACTS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _ac(contract: dict[str, Any], ac_id: str) -> str:
    for requirement in contract["requirements"]:
        for criterion in requirement["acceptance"]:
            if criterion["id"] == ac_id:
                return str(criterion["statement"])
    raise AssertionError(ac_id)


def test_contract_omn_24827_ac1_reads_each_bound_selection_exits_non_zero_at_base() -> (
    None
):
    contract = _load("OMN-24827.yaml")
    statement = _ac(contract, "AC1")
    assert "each bound selection" in statement
    assert "exits non-zero" in statement
    assert "every bound test" not in statement
    assert "-- falsifier:" in statement
    assert statement.rstrip().endswith(
        "exits zero with the merge-base outbox module loaded"
    )


def test_contract_omn_24827_ac1_names_one_selection_everywhere() -> None:
    contract = _load("OMN-24827.yaml")
    selections = {
        m.group(1)
        for text in (
            _ac(contract, "AC1"),
            *(req["command"] for req in contract["evidence_requirements"]),
            *(
                check["check_value"]
                for item in contract["dod_evidence"]
                if "AC1" in item.get("binds_ac", [])
                for check in item["checks"]
            ),
        )
        for m in SELECTION.finditer(text)
    }
    assert selections == {"stored_refusal_lookup or no_new_report"}


def test_contract_omn_24250_ac2_description_says_it_is_a_preservation_criterion() -> (
    None
):
    contract = _load("OMN-24250.yaml")
    (item,) = [i for i in contract["dod_evidence"] if i["id"] == "dod-omn24250-ac2"]
    assert "preservation criterion" in item["description"]
