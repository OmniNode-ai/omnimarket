# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26336: the contract binds only the criteria this change makes true.

The hosted dod-verify must-fail control runs every bound test at the merge
base and refuses a bound test that already passes there. The delegation
lazy-import test is already carried by the landing branch, so binding it in
this contract turned repo-evidence red as an always-pass control.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26336.yaml"
ALREADY_LANDED_TEST = "test_delegation_boot_lazy_import_omn26336"


def _load() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_acceptance_ids_are_the_ticket_criteria() -> None:
    contract = _load()
    ids: list[str] = []
    requirements = cast(
        "list[dict[str, list[dict[str, str]]]]", contract["requirements"]
    )
    for requirement in requirements:
        for criterion in requirement["acceptance"]:
            ids.append(criterion["id"])
    assert sorted(ids) == ["AC1", "AC2", "AC3", "AC4"]


def test_contract_binds_no_test_the_landing_branch_already_passes() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert ALREADY_LANDED_TEST not in text
