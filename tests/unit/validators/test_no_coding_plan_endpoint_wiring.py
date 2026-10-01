# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173 Rule 5: the endpoint gate is enforced before commit and merge."""

import ast
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
ENTRY = "uv run python -m omnimarket.validators.no_coding_plan_endpoint"


def test_precommit_wiring() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        h
        for repo in config["repos"]
        for h in repo["hooks"]
        if h["id"] == "no-coding-plan-endpoint"
    )
    assert hook["entry"] == ENTRY
    assert hook["pass_filenames"] is False
    assert hook["stages"] == ["pre-commit"]
    for filename in (
        "contract.yaml",
        "docker-compose.yml",
        "config.json",
        "pyproject.toml",
        ".env",
        ".env.local",
        "service.env.template",
        "docker-compose",
        "src/omnimarket/validators/no_coding_plan_endpoint.py",
    ):
        assert re.search(hook["files"], filename), filename


def test_ci_wiring() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/no-coding-plan-endpoint.yml").read_text()
    )
    assert workflow[True]["pull_request"]["branches"] == ["main", "dev"]
    job = workflow["jobs"]["no-coding-plan-endpoint"]
    assert job["name"] == "No Coding Plan Endpoint"
    steps = [step.get("run", "") for step in job["steps"]]
    assert ENTRY in steps
    assert any("test_no_coding_plan_endpoint.py" in step for step in steps)
    module = ast.parse((ROOT / "scripts/ci/ci_summary_gate.py").read_text())
    contexts = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "EXPECTED_EXTERNAL_CONTEXTS"
        and node.value is not None
    )
    assert "No Coding Plan Endpoint" in contexts
