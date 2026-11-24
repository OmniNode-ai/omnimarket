"""OMN-26488: the accepted AC14 evidence item of OMN-22519 declares its pytest run as test_passes.

The item ``dod-omn22519-ac14-accepted-20261124t1145z`` in ``contracts/OMN-22519.yaml``
binds AC14 to ``test_public_review_receipt.py``. With ``check_type: command`` the
binding check does not read the ``uv run --frozen pytest`` line as a test run, so the
repo contract binding test went red on dev. The item must declare the run as a
``test_passes`` check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contracts" / "OMN-22519.yaml"
AC14_ITEM_ID = "dod-omn22519-ac14-accepted-20261124t1145z"
AC14_TEST_FILE = "tests/unit/nodes/node_agent_review_gate_accept_effect/test_public_review_receipt.py"


def _ac14_item() -> dict[str, Any]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    matches = [
        item for item in contract["dod_evidence"] if item.get("id") == AC14_ITEM_ID
    ]
    assert len(matches) == 1, (
        f"expected exactly one {AC14_ITEM_ID} item in {CONTRACT_PATH.name}, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_omn22519_ac14_accepted_item_declares_its_pytest_run_as_test_passes() -> None:
    item = _ac14_item()
    assert item["binds_ac"] == ["AC14"]

    pytest_checks = [
        check
        for check in item["checks"]
        if AC14_TEST_FILE in str(check.get("check_value", ""))
    ]
    assert pytest_checks, f"{AC14_ITEM_ID} has no check running {AC14_TEST_FILE}"

    wrong_type = [
        check["check_type"]
        for check in pytest_checks
        if check["check_type"] != "test_passes"
    ]
    assert not wrong_type, (
        f"{AC14_ITEM_ID} declares its pytest run as check_type {wrong_type}; "
        "it must be test_passes so the binding check reads the test file"
    )
    assert AC14_TEST_FILE in str(pytest_checks[0]["check_value"])
    assert (REPO_ROOT / AC14_TEST_FILE).is_file()
