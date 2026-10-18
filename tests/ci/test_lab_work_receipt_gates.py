# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt acceptance tests gate commits and required CI (OMN-20278)."""

import re
import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("hook_id", "test_file"),
    [
        (
            "lab-work-receipt-mapping",
            "tests/unit/nodes/node_work_ledger_bus_mirror/test_lab_work_receipt_mapping.py",
        ),
        (
            "lab-work-receipt-redelivery",
            "tests/unit/work_ledger_bus/test_lab_work_receipt_mirror.py",
        ),
        ("lab-work-receipt-gates", "tests/ci/test_lab_work_receipt_gates.py"),
    ],
)
def test_lab_work_receipt_gates_run_acceptance_files(
    hook_id: str, test_file: str
) -> None:
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    selected = [
        hook
        for repo in hooks["repos"]
        for hook in repo["hooks"]
        if hook["id"] == hook_id
    ]
    assert len(selected) == 1
    hook = selected[0]
    assert hook["pass_filenames"] is False
    assert hook["stages"] == ["pre-commit"]
    command = shlex.split(hook["entry"])
    assert command[:5] == ["uv", "run", "--frozen", "pytest", test_file]
    assert "-k" not in command
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = [
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("run", "").strip() == hook["entry"]
    ]
    assert len(steps) == 1
    assert not steps[0].get("continue-on-error", False)
    assert not workflow["jobs"]["lint"].get("continue-on-error", False)
    required = yaml.safe_load((ROOT / ".github/required-checks.yaml").read_text())
    assert any(
        gate["name"] == "lint" and gate["mode"] == "REQUIRED"
        for gate in required["gates"]
    )
    # Mapping, either producer and the host's dedup path must trigger every hook.
    for path in (
        "src/omnimarket/models/model_work_ledger_bus_mirror_request.py",
        "src/omnimarket/nodes/node_work_ledger_bus_mirror/contract.yaml",
        "src/omnimarket/nodes/node_lab_work_unit_effect/models/model_lab_work_unit.py",
        "src/omnimarket/lab_work/bus.py",
        "src/omnimarket/work_ledger_bus/bus.py",
        "src/omnimarket/nodes/node_work_ledger_append_effect/handlers/handler_work_ledger_append_effect.py",
        test_file,
        "tests/ci/test_lab_work_receipt_gates.py",
        "contracts/OMN-20278.yaml",
        ".github/workflows/ci.yml",
        ".pre-commit-config.yaml",
    ):
        assert re.search(hook["files"], path), path
    assert not re.search(hook["files"], "docs/unrelated.md")
