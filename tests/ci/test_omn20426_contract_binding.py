# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23327: contracts/OMN-20426.yaml binds every criterion of OMN-20426.

The capability_scores writer merged in omnimarket#3346 and was proved on the dev
lane, but the ticket's contract bound no evidence, so the ticket closer had no
receipt to close it on. These tests read the contract the way the closer does:
the contract must exist, declare the three criteria, and bind each of them to a
test_passes check that names a test file that exists in this repository.
"""

from __future__ import annotations

import copy
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contracts" / "OMN-20426.yaml"
EXPECTED_CRITERIA = ["AC1", "AC2", "AC3"]


def _load() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "contracts/OMN-20426.yaml must be a mapping"
    return loaded


def _criteria(contract: dict[str, Any]) -> list[str]:
    return [
        acceptance["id"]
        for requirement in contract["requirements"]
        for acceptance in requirement["acceptance"]
    ]


def _test_files(check_value: str) -> list[str]:
    return [part for part in shlex.split(check_value) if part.endswith(".py")]


def unbound_criteria(contract: dict[str, Any]) -> list[str]:
    """Criteria with no dod_evidence entry that binds them to a test_passes check
    naming an existing test file."""
    bound: set[str] = set()
    for entry in contract["dod_evidence"]:
        checks = entry.get("checks", [])
        names_real_test = any(
            check["check_type"] == "test_passes"
            and _test_files(check["check_value"])
            and all(
                (REPO_ROOT / name).is_file()
                for name in _test_files(check["check_value"])
            )
            for check in checks
        )
        if names_real_test:
            bound.update(entry.get("binds_ac", []))
    return [ac for ac in _criteria(contract) if ac not in bound]


def test_contract_names_the_ticket_and_declares_three_criteria() -> None:
    contract = _load()
    assert contract["ticket_id"] == "OMN-20426"
    assert _criteria(contract) == EXPECTED_CRITERIA


def test_every_criterion_is_bound_to_an_existing_test() -> None:
    assert unbound_criteria(_load()) == []


def test_unbinding_a_criterion_is_detected() -> None:
    """Injected break: drop AC3 from every binding; the reading must name AC3."""
    broken = copy.deepcopy(_load())
    for entry in broken["dod_evidence"]:
        entry["binds_ac"] = [ac for ac in entry.get("binds_ac", []) if ac != "AC3"]
    assert unbound_criteria(broken) == ["AC3"]


def test_naming_a_missing_test_file_is_detected() -> None:
    """Injected break: point every check at a test file that does not exist."""
    broken = copy.deepcopy(_load())
    for entry in broken["dod_evidence"]:
        for check in entry.get("checks", []):
            check["check_value"] = check["check_value"].replace(
                "test_canary_score_reducer_writes_capability_scores.py",
                "test_no_such_file.py",
            )
    assert unbound_criteria(broken) == EXPECTED_CRITERIA
