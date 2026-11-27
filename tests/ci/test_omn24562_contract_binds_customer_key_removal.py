# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pull request titled OMN-24562 binds its customer-key work in OMN-24562's own contract.

repo-evidence / dod-verify refuses a pull request whose ticket contract carries only
evidence items already on dev. This pins the new criterion and its evidence item.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24562.yaml"
KEY_TESTS = (
    "tests/test_credential_publisher.py",
    "tests/unit/cli/test_omn24562_revoke_tenant_key_command.py",
    "tests/unit/cli/test_omn21012_register_tenant_key_host.py",
)


REQUIREMENT_ID = "req-omn24562-customer-key-removal"
EVIDENCE_ID = "dod-omn24562-customer-key-removal"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def _by_id(rows: list[dict], row_id: str) -> dict:
    """The one row with this id; a missing or repeated id is a failure, not a guess."""
    found = [r for r in rows if r.get("id") == row_id]
    assert len(found) == 1, (
        f"expected exactly one row with id {row_id}, got {len(found)}"
    )
    return found[0]


def _key_tests_missing(contract: dict) -> list[str]:
    """Key tests the customer-key evidence item (picked by its id) does not run.

    The item is picked by its id and never by position or by an acceptance id: other
    changes append items and take acceptance ids on the shared contract.
    """
    item = _by_id(contract["dod_evidence"], EVIDENCE_ID)
    commands = " ".join(c["check_value"] for c in item["checks"])
    return [path for path in KEY_TESTS if path not in commands]


def test_contract_declares_the_customer_key_removal_criterion() -> None:
    requirement = _by_id(_contract()["requirements"], REQUIREMENT_ID)
    assert requirement.get("acceptance"), (
        "the customer-key requirement has no criterion"
    )


def test_the_evidence_item_binds_a_criterion_of_the_requirement() -> None:
    contract = _contract()
    requirement = _by_id(contract["requirements"], REQUIREMENT_ID)
    item = _by_id(contract["dod_evidence"], EVIDENCE_ID)
    declared = {ac["id"] for ac in requirement["acceptance"]}
    assert item.get("binds_ac")
    assert set(item["binds_ac"]) <= declared


def test_a_new_evidence_item_binds_it_to_the_key_tests() -> None:
    assert _key_tests_missing(_contract()) == []


def test_an_item_with_the_right_id_but_without_the_key_tests_fails() -> None:
    contract = _contract()
    item = _by_id(contract["dod_evidence"], EVIDENCE_ID)
    item["checks"] = [
        {
            "check_type": "test_passes",
            "check_value": "uv run pytest tests/unit/test_unrelated.py -q",
            "cwd": ".",
        }
    ]
    assert _key_tests_missing(contract) == list(KEY_TESTS)


def test_a_later_item_bound_to_the_same_criterion_does_not_change_the_pick() -> None:
    contract = _contract()
    item = _by_id(contract["dod_evidence"], EVIDENCE_ID)
    contract["dod_evidence"].append(
        {
            "id": "dod-omn24562-appended-later",
            "binds_ac": list(item["binds_ac"]),
            "checks": [{"check_type": "test_passes", "check_value": "uv run pytest x"}],
        }
    )
    assert _key_tests_missing(contract) == []
