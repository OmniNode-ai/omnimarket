# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22496: every check in contracts/OMN-22496.yaml runs inside ${OMNI_HOME}/omnimarket.

The evidence sweep resolved cwd "." to its sparse contract-roots export, which holds
only contracts/, so uv could not build omnimarket there and every check failed.
"""

from __future__ import annotations

import pathlib
import typing

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-22496.yaml"
REQUIRED_CWD = "${OMNI_HOME}/omnimarket"


def checks_outside_omni_home(contract: dict[str, typing.Any]) -> list[str]:
    """Return descriptions of checks whose cwd is missing or not under REQUIRED_CWD."""
    violations: list[str] = []
    for item in contract.get("dod_evidence", []):
        item_id = item.get("id", "unknown")
        for index, check in enumerate(item.get("checks", [])):
            cwd = check.get("cwd")
            if cwd is None or (
                cwd != REQUIRED_CWD and not cwd.startswith(REQUIRED_CWD + "/")
            ):
                violations.append(f"{item_id} check {index}: cwd={cwd}")
    return violations


def test_every_omn22496_check_runs_inside_omni_home() -> None:
    with CONTRACT.open(encoding="utf-8") as f:
        contract = yaml.safe_load(f)
    assert len(contract.get("dod_evidence", [])) >= 1
    assert checks_outside_omni_home(contract) == []


def test_a_check_with_cwd_dot_is_named() -> None:
    contract = {
        "dod_evidence": [
            {
                "id": "dod-planted",
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests -q",
                        "cwd": ".",
                    }
                ],
            }
        ]
    }
    assert checks_outside_omni_home(contract) == ["dod-planted check 0: cwd=."]


def test_a_check_with_no_cwd_is_named() -> None:
    contract = {
        "dod_evidence": [
            {
                "id": "dod-planted",
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests -q",
                    }
                ],
            }
        ]
    }
    assert checks_outside_omni_home(contract) == ["dod-planted check 0: cwd=None"]
