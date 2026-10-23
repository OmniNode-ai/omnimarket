# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19320 evidence binds every acceptance criterion of the ticket to a test that exists."""

import ast
import shlex
from pathlib import Path

import pytest
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "OMN-19320.yaml"
CRITERIA = {"AC1", "AC2", "AC3", "AC4"}


def _load() -> ModelTicketContract:
    return ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))


def test_every_criterion_is_claimed_by_an_item_with_a_check() -> None:
    contract = _load()
    claimed: set[str] = set()
    for item in contract.dod_evidence:
        if item.binds_ac:
            claimed.update(item.binds_ac)
            assert item.checks, item.id
    assert claimed == CRITERIA


def test_each_criterion_names_its_own_test_class() -> None:
    contract = _load()
    classes: dict[str, str] = {
        "AC1": "TestEveryCheckCarriesItsClassAC1",
        "AC2": "TestThePairAC2",
        "AC3": "TestUndersizedCorporaAreRefusedAC3",
        "AC4": "TestPinnedRunsAreRefusedAC4",
    }
    for item in contract.dod_evidence:
        for label in item.binds_ac:
            assert any(classes[label] in check.check_value for check in item.checks), (
                item.id
            )


def test_every_bound_selector_names_a_test_that_exists() -> None:
    contract = _load()
    for item in contract.dod_evidence:
        if not item.binds_ac:
            continue
        node_ids: list[str] = []
        for check in item.checks:
            node_ids.extend(
                token
                for token in shlex.split(check.check_value)
                if "::" in token or token.endswith(".py")
            )
        assert node_ids, item.id
        for node_id in node_ids:
            parts = node_id.split("::")
            path = ROOT / parts[0]
            assert path.is_file(), node_id
            if len(parts) == 1:
                continue
            assert len(parts) == 3, node_id
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            test_class = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == parts[1]
                ),
                None,
            )
            assert test_class is not None, node_id
            assert any(
                isinstance(node, ast.FunctionDef) and node.name == parts[2]
                for node in test_class.body
            ), node_id
