# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket's deploy gate reads this repository's own contracts (OMN-20074).

omnimarket is cut over: its evidence check is repo-evidence / dod-verify over
``contracts/OMN-<n>.yaml`` at the pull request head. The deploy gate must read
the same file. A runtime-path PR whose cited ticket carries deploy evidence in
that file passes; one whose repo contract lacks it fails, even when a change
control companion carries the evidence, because no change control tree is
checked out. The validator's own strictness (falsifiability, the grandfather
snapshot) is the pinned omniclaude action's and is unchanged.

These tests read this repository's own workflow, so they run on every runner
with no omniclaude or change control checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-gate.yml"
pytestmark = pytest.mark.unit

DEPLOY_GATE_ACTION = "OmniNode-ai/omniclaude/.github/actions/deploy-gate@"
PR_HEAD_SHA = "${{ github.event.pull_request.head.sha }}"
CHANGE_CONTROL_MARKERS = (
    "onex_change_control",
    "_occ",
    "--resolve-occ-ref",
    "--default-occ-ref",
    "checkout-occ-contracts",
    "OCC_REF",
)


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy-gate"]["steps"]
    assert isinstance(steps, list)
    return steps


def _gate_step() -> dict[str, Any]:
    gates = [
        s for s in _steps() if str(s.get("uses", "")).startswith(DEPLOY_GATE_ACTION)
    ]
    assert len(gates) == 1, gates
    return gates[0]


def _head_contracts_checkout() -> dict[str, Any]:
    checkouts = [
        s
        for s in _steps()
        if str(s.get("uses", "")).startswith("actions/checkout@")
        and (s.get("with") or {}).get("ref") == PR_HEAD_SHA
    ]
    assert len(checkouts) == 1, (
        "deploy-gate.yml must check out the pull request head exactly once "
        f"to read its contracts; found {len(checkouts)}"
    )
    return checkouts[0]


def test_no_step_reads_change_control() -> None:
    """A cut-over repository's deploy gate has no change control source at all."""
    text = WORKFLOW.read_text()
    found = [marker for marker in CHANGE_CONTROL_MARKERS if marker in text]
    assert not found, f"deploy-gate.yml still reads change control: {found}"


def test_head_checkout_is_the_repo_itself_and_carries_contracts() -> None:
    checkout = _head_contracts_checkout()
    options = checkout["with"]
    assert "repository" not in options, options
    assert options.get("persist-credentials") is False, options
    sparse = str(options.get("sparse-checkout", "")).split()
    assert sparse == ["/contracts/"], sparse
    assert options.get("path"), "the head contracts need their own path"


def test_validator_reads_the_pr_head_contracts_dir() -> None:
    """The contracts-dir handed to the validator is the PR head's contracts/."""
    head_path = _head_contracts_checkout()["with"]["path"]
    contracts_dir = _gate_step()["with"]["contracts-dir"]
    assert contracts_dir == f"{head_path}/contracts", contracts_dir


def test_head_checkout_runs_before_the_gate_on_every_pr_event() -> None:
    steps = _steps()
    checkout = _head_contracts_checkout()
    gate = _gate_step()
    assert steps.index(checkout) < steps.index(gate)
    assert checkout.get("if") == gate.get("if"), (checkout.get("if"), gate.get("if"))


def test_validator_strictness_is_the_pinned_action() -> None:
    """Same validator and same mode as before the cut-over: only the source moved."""
    gate = _gate_step()
    assert gate["uses"].startswith(DEPLOY_GATE_ACTION)
    assert "falsifiability-mode" not in gate["with"], gate["with"]
