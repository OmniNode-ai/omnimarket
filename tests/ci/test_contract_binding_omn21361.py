# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21361: every criterion-bound evidence item of the contract names a repo test."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ci.test_repo_evidence_caller_workflow import contract_binds_violations

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-21361.yaml"


def test_the_omn21361_contract_satisfies_the_binding_rule() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract_binds_violations(CONTRACT.name, contract) == []


def test_the_deploy_probe_item_runs_the_legacy_column_tests() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    item = next(
        item
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn21361-deploy-probe-store-step"
    )
    assert item["binds_ac"] == ["AC1", "AC2"]
    tests = [
        check["check_value"]
        for check in item["checks"]
        if check["check_type"] == "test_passes"
    ]
    assert tests == [
        "uv run pytest tests/unit/projection/test_sqlite_metering_summary_legacy_column_omn21361.py -q --no-cov"
    ]
