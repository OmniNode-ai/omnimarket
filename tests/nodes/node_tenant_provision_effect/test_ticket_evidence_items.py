# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket contract's Postgres evidence items each run one test, so each fits the per-check budget.

The done gate kills a check at its per-check ceiling and records it unverifiable,
which blocks the Done flip. The seam-shape item that ran two Postgres tests in one
command was killed at that ceiling, so each test has its own item, and any lab work
unit in the contract starts with an executable the lab handler runs.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-26473.yaml"
POSTGRES_FILE = "tests/nodes/node_tenant_provision_effect/test_provision_postgres.py"


def _items() -> list[dict[str, Any]]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = loaded["dod_evidence"]
    return items


def _commands(item: dict[str, Any]) -> list[str]:
    return [check["check_value"] for check in item.get("checks", [])]


def test_each_postgres_item_runs_one_selected_test() -> None:
    seen = 0
    for item in _items():
        for command in _commands(item):
            if POSTGRES_FILE not in command:
                continue
            seen += 1
            assert " or " not in command, (
                f"{item['id']} selects several Postgres tests in one command: {command}"
            )
    assert seen, "the contract binds no Postgres test item"


def test_the_seam_shape_tests_each_have_an_item() -> None:
    commands = [c for item in _items() for c in _commands(item)]
    for selector in ("test_b_double_run", "test_read_answers_by_id"):
        assert any(POSTGRES_FILE in c and selector in c for c in commands), (
            f"no evidence item runs {selector} on its own"
        )


def test_lab_work_units_start_with_an_executable_the_lab_runs() -> None:
    for item in _items():
        for command in _commands(item):
            words = shlex.split(command)
            if "lab-work" in words and "--" in words:
                argv = words[words.index("--", words.index("lab-work")) + 1 :]
                assert argv[0] in DEFAULT_ALLOWED_EXECUTABLES, (
                    f"{item['id']}: the lab refuses {argv[0]!r}"
                )
