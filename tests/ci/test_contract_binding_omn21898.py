# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21898: the criterion-bound AC1 item names the node's test files, not its directory."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ci.test_contract_evidence_selector_names_file_omn17427 import (
    _pytest_selector,
)
from tests.ci.test_repo_evidence_caller_workflow import contract_binds_violations

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-21898.yaml"
NODE_TESTS = "tests/nodes/node_prompt_intent_classify_compute"
NODE_TEST_FILES = (
    f"{NODE_TESTS}/test_handler_prompt_intent_classify.py",
    f"{NODE_TESTS}/test_omn21898_omniintelligence_surface.py",
)


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _ac1_item() -> dict[str, object]:
    items = _contract()["dod_evidence"]
    assert isinstance(items, list)
    return next(item for item in items if item["id"] == "dod-omn21898-ac1")


def test_the_omn21898_contract_satisfies_the_binding_rule() -> None:
    assert contract_binds_violations(CONTRACT.name, _contract()) == []


def test_ac1_runs_every_test_file_of_the_node_by_name() -> None:
    item = _ac1_item()
    assert item["binds_ac"] == ["AC1"]
    checks = item["checks"]
    assert isinstance(checks, list)
    assert [check["check_value"] for check in checks] == [
        "uv run pytest " + " ".join(NODE_TEST_FILES) + " -q --no-cov --tb=line"
    ]
    assert _pytest_selector(checks[0]["check_value"]) == NODE_TEST_FILES[0]


def test_the_named_files_are_every_test_file_in_the_node_directory() -> None:
    on_disk = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / NODE_TESTS).glob("test_*.py")
    )
    assert on_disk == sorted(NODE_TEST_FILES)
