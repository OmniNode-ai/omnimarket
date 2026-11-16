# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The eval-results contracts bind each criterion to a check that can fail for it (OMN-24914).

AC5 of the projection ticket is an integrated, dev-lane criterion: it must be bound to the SQL count
by run id, not to a rerun of the tests that bind AC1 and AC4. AC2 of the unblock ticket is a ledger
claim: it must be bound to a ledger-row check, not to the node tests. Every test id the node
contract names must exist.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_NODE_CONTRACT = (
    _ROOT / "src/omnimarket/nodes/node_projection_eval_results/contract.yaml"
)


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_ROOT / "contracts" / name).read_text(encoding="utf-8"))


def _item_for(contract: dict[str, Any], ac: str) -> dict[str, Any]:
    (item,) = [i for i in contract["dod_evidence"] if ac in i.get("binds_ac", [])]
    return item


def _checks(item: dict[str, Any]) -> list[str]:
    return [str(c["check_value"]) for c in item["checks"]]


def test_omn24171_ac5_is_bound_to_the_dev_sql_count_by_run_id() -> None:
    contract = _load("OMN-24171.yaml")
    item = _item_for(contract, "AC5")
    (check,) = item["checks"]
    assert check["check_type"] == "command"
    assert item["execution_scope"] == "local_done_gate"
    command = check["check_value"]
    for needle in (
        "eval_item_scores",
        "context_roi_scores",
        "group by run_id",
        "1/(case when",
    ):
        assert needle in command, needle


def test_omn24171_ac5_is_not_a_rerun_of_the_ac1_and_ac4_tests() -> None:
    contract = _load("OMN-24171.yaml")
    ac5 = _item_for(contract, "AC5")
    rerun = set(_checks(_item_for(contract, "AC1"))) | set(
        _checks(_item_for(contract, "AC4"))
    )
    assert not set(_checks(ac5)) & rerun
    assert all(c["check_type"] != "test_passes" for c in ac5["checks"])
    # The files AC1 and AC4 are bound to appear nowhere in AC5's checks, however the command is spelt.
    for source in (
        "test_golden_chain_projection_eval_results",
        "test_snapshot.py",
        "test_writer.py",
    ):
        assert source not in " ".join(_checks(ac5))


def test_omn24802_ac2_is_bound_to_a_ledger_row_check_not_the_node_tests() -> None:
    contract = _load("OMN-24802.yaml")
    item = _item_for(contract, "AC2")
    (check,) = item["checks"]
    assert check["check_type"] == "command"
    assert item["execution_scope"] == "local_done_gate"
    command = check["check_value"]
    for needle in (
        "lane_brief.py",
        "STATUS",
        "omnimarket#6204",
        "cause=",
        "shared=yes",
    ):
        assert needle in command, needle
    assert "pytest" not in command


def _functions_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_omn24171_every_test_id_the_node_contract_names_exists() -> None:
    contract = yaml.safe_load(_NODE_CONTRACT.read_text(encoding="utf-8"))
    ids = [e["test"] for e in contract["dod_evidence"] if "test" in e]
    assert ids
    for test_id in ids:
        path, _, name = test_id.partition("::")
        assert (_ROOT / path).is_file(), f"{path} does not exist"
        assert name in _functions_of(_ROOT / path), f"{test_id} names no test"


def test_omn24171_every_pytest_path_a_contract_runs_exists() -> None:
    for name in ("OMN-24171.yaml", "OMN-24802.yaml"):
        contract = _load(name)
        commands = [
            c["check_value"]
            for item in contract["dod_evidence"]
            for c in item["checks"]
            if c["check_type"] == "test_passes"
        ]
        assert commands
        for command in commands:
            for token in command.split():
                if token.startswith("tests/"):
                    assert (_ROOT / token).exists(), f"{name}: {token} does not exist"
