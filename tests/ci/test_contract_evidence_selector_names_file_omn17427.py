# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: bound pytest evidence names an existing repo test file or tests directory."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pytest_selector(command: str) -> str:
    tokens = shlex.split(command)
    selector = next((token for token in tokens[3:] if not token.startswith("-")), "")
    return selector.split("::", 1)[0]


def test_every_bound_pytest_check_names_a_repo_test_file_or_tests_directory() -> None:
    contracts = sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml"))
    assert contracts, "expected at least one repo-owned contracts/OMN-*.yaml"
    offenders: list[str] = []
    for path in contracts:
        contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        for item in contract.get("dod_evidence", []):
            if "binds_ac" not in item:
                continue
            for check in item.get("checks", []):
                if check.get("check_type") != "test_passes" or not check.get(
                    "check_value", ""
                ).startswith("uv run pytest "):
                    continue
                selector = _pytest_selector(check["check_value"])
                target = REPO_ROOT / selector
                valid = (
                    bool(selector)
                    and not Path(selector).is_absolute()
                    and target.resolve().is_relative_to(REPO_ROOT)
                    and (
                        (selector.endswith(".py") and target.is_file())
                        or (selector.endswith("/tests") and target.is_dir())
                    )
                )
                if not valid:
                    offenders.append(
                        f"{path.name}:{item['id']}: {selector}: "
                        "expected an existing repo .py file or /tests directory; "
                        "a bare directory selector is refused by the repo evidence gate"
                    )
    assert not offenders, "\n".join(offenders)


def test_omn20429_ac4_names_the_existing_golden_chain_test_file() -> None:
    path = REPO_ROOT / "contracts" / "OMN-20429.yaml"
    contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    item = next(
        item for item in contract["dod_evidence"] if item["id"] == "dod-omn-20429-ac4"
    )
    checks = [
        check
        for check in item.get("checks", [])
        if check.get("check_type") == "test_passes"
        and check.get("check_value", "").startswith("uv run pytest ")
    ]
    expected = (
        "tests/golden_chains/delegation_acceptance_judge/"
        "test_golden_chain_delegation_acceptance_judge.py"
    )
    assert checks, "OMN-20429.yaml:dod-omn-20429-ac4: expected a pytest check"
    assert all(_pytest_selector(check["check_value"]) == expected for check in checks)
    assert (REPO_ROOT / expected).is_file()


def test_omn20890_park_ladder_checks_name_the_causes_test_file() -> None:
    path = REPO_ROOT / "contracts" / "OMN-20890.yaml"
    contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = (
        "tests/unit/nodes/node_pr_landing_decision_compute/"
        "test_landing_decision_causes.py"
    )
    selectors = [
        _pytest_selector(check["check_value"])
        for item in contract["dod_evidence"]
        if item["id"].startswith(
            (
                "dod-omn20890-ac1-third",
                "dod-omn20890-ac2",
                "dod-omn20890-ac3",
                "dod-omn20890-ac4",
            )
        )
        for check in item.get("checks", [])
        if check.get("check_value", "").startswith("uv run pytest ")
    ]
    assert len(selectors) == 4, selectors
    assert set(selectors) == {expected}
    assert (REPO_ROOT / expected).is_file()


def test_omn21335_compact_binding_checks_name_the_test_file_and_bind_tests() -> None:
    path = REPO_ROOT / "contracts" / "OMN-21335.yaml"
    contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = (
        "tests/nodes/node_claude_code_hook_adapter_effect/"
        "test_adapter_template_compact_binding.py"
    )
    items = {item["id"]: item for item in contract["dod_evidence"]}
    for item_id in (
        "dod-omn21335-ac1-template-tests",
        "dod-omn21335-ac2-install-readback",
    ):
        selectors = [
            _pytest_selector(check["check_value"])
            for check in items[item_id].get("checks", [])
            if check.get("check_type") == "test_passes"
        ]
        assert selectors == [expected], (item_id, selectors)
    # A command-only deploy probe is evidence the verifier runs, not a binding.
    assert "binds_ac" not in items["dod-omn21335-deploy-render-runtime"]
    assert (REPO_ROOT / expected).is_file()
