# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478, operator RULING 2026-11-05T20:27:56Z: no automated review holds a merge.

A head with the reviewer's rows red or absent must pass CI Summary's external layers,
the reviewer must start no run on a pull request, and the manifest must not call its
contexts required. The static PR Arch Review Gate is no model and stays required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 5, 21, 0, 0, tzinfo=UTC)
REVIEW_CONTEXTS = (
    "Hostile Reviewer (adversarial gate)",
    "Hostile Review Gate",
    "Hostile Review Thread Gate",
)


def _row(name: str, conclusion: str) -> dict[str, object]:
    return {
        "id": abs(hash(name)) % 10_000_000,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-11-05T20:00:00Z",
        "completed_at": "2026-11-05T20:05:00Z",
        "head_sha": "a" * 40,
    }


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", None])
def test_external_layers_pass_with_the_reviewer_rows_red_or_absent(
    conclusion: str | None,
) -> None:
    rows = [_row(name, "success") for name in gate.EXPECTED_EXTERNAL_CONTEXTS]
    if conclusion is not None:
        rows += [_row(name, conclusion) for name in REVIEW_CONTEXTS]
    code, report = gate.evaluate_external(rows, now=NOW)
    assert code == gate.EXIT_SUCCESS, report
    code, report = gate.evaluate_external_sweep(rows, now=NOW)
    assert code == gate.EXIT_SUCCESS, report


def test_no_review_context_is_asserted() -> None:
    assert not set(REVIEW_CONTEXTS) & set(gate.EXPECTED_EXTERNAL_CONTEXTS)


def test_the_manifest_keeps_the_static_arch_gate_and_no_review_context() -> None:
    manifest = yaml.safe_load((REPO / ".github/required-checks.yaml").read_text())
    required = {g["name"] for g in manifest["gates"] if g.get("mode") == "REQUIRED"}
    assert not set(REVIEW_CONTEXTS) & required
    assert "PR Arch Review Gate" in required


def test_the_reviewer_starts_no_run_on_a_pull_request() -> None:
    data: dict[Any, Any] = yaml.safe_load(
        (REPO / ".github/workflows/hostile-reviewer.yml").read_text()
    )
    branches = data.get("on", data.get(True))["pull_request"]["branches"]
    assert not {"dev", "main"} & set(branches)


def test_the_queue_mirrors_no_review_context() -> None:
    data: dict[Any, Any] = yaml.safe_load(
        (REPO / ".github/workflows/merge-group-pr-scoped-contexts.yml").read_text()
    )
    names = {job.get("name") for job in data["jobs"].values()}
    assert not set(REVIEW_CONTEXTS) & names
