# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21885: every criterion-bound evidence item of the contract names a repo test."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ci.test_repo_evidence_caller_workflow import contract_binds_violations

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-21885.yaml"
LEDGER_TESTS = "tests/unit/nodes/node_projection_work_ledger"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_the_omn21885_contract_satisfies_the_binding_rule() -> None:
    assert contract_binds_violations(CONTRACT.name, _contract()) == []


@pytest.mark.parametrize(
    ("item_id", "binds_ac", "test_file", "selector"),
    [
        ("dod-omn21885-by-kind", ["AC1"], "test_parity_by_kind.py", "by_kind"),
        (
            "dod-omn21885-verb-diff",
            ["AC2"],
            "test_parity_verb_diff.py",
            "verb_diff",
        ),
        (
            "dod-omn21885-m6-fixture",
            ["AC3"],
            "test_parity_by_kind.py",
            "by_kind_23_missing_status",
        ),
    ],
)
def test_each_bound_item_runs_the_test_file_its_selector_selects(
    item_id: str, binds_ac: list[str], test_file: str, selector: str
) -> None:
    contract = _contract()
    items = contract["dod_evidence"]
    assert isinstance(items, list)
    item = next(item for item in items if item["id"] == item_id)
    assert item["binds_ac"] == binds_ac
    assert [check["check_value"] for check in item["checks"]] == [
        "env PYTHONDONTWRITEBYTECODE=1 uv run pytest "
        f"{LEDGER_TESTS}/{test_file} -k {selector} -q --no-cov"
    ]
