# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22176: the workflow adds no decision logic -- one node call from the base commit, and one ``if:`` on the enforcement step.

The gate-accept step checks out the pull request's base commit into a worktree and runs
the node's ``python -m`` entry from there, as the finding veto does, so a pull request
can never supply the code that accepts itself. Its shell holds only that checkout, the
node call and the ``decision=unchanged`` fallbacks for a base without the node. The
"Enforce hostile-review verdict" step keeps its ``run:`` text byte for byte (pinned by
digest, taken from the default branch) and gains only the condition that skips it on
``pass_agent``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import OVERLAY_ID
from tests.ci.hostile_review_job import REVIEW_JOB, job, step_named

pytestmark = pytest.mark.unit

#: sha256 of the enforcement step's ``run:`` text on the default branch before OMN-22176.
ENFORCE_RUN_SHA256 = "458e14487bec17542a6779907ed147440f2062d4084779f0827281bd285b9b91"
ENFORCE_IF = "always() && steps.accept.outputs.decision != 'pass_agent'"
ACCEPT_CONTEXTS = {
    "github.repository",
    "github.event.pull_request.number",
    "github.event.pull_request.head.sha",
    "github.event.pull_request.base.sha",
    "github.workspace",
    "runner.temp",
    "secrets.GITHUB_TOKEN",
}
NODE_MODULE = (
    "omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept"
)


def _steps() -> list[dict[str, Any]]:
    return list(job(REVIEW_JOB)["steps"])


def _accept() -> dict[str, Any]:
    found = [s for s in _steps() if s.get("id") == "accept"]
    assert len(found) == 1, "the job must hold exactly one gate-accept step"
    return found[0]


def test_the_accept_step_runs_the_node_from_the_base_commit_under_if_always() -> None:
    step = _accept()
    assert set(step) == {"name", "id", "if", "env", "run"}, sorted(step)
    assert step["if"] == "always()"
    run = str(step["run"])
    assert "onex run-node" not in run
    assert 'worktree add --detach "$ACCEPT_BASE" "$ACCEPT_BASE_SHA"' in run
    calls = re.findall(rf"python -m {re.escape(NODE_MODULE)}\b", run)
    assert len(calls) == 1, calls
    assert (
        'PYTHONPATH="$ACCEPT_BASE/src" uv run --no-sync python -m ' + NODE_MODULE
    ) in run
    assert "PYTHONPATH" not in run.replace('PYTHONPATH="$ACCEPT_BASE/src"', "")
    assert "$GITHUB_WORKSPACE/src" not in run
    assert "::notice::" in run, "a base without the node must skip with a notice"
    assert run.count("decision=unchanged") == 3, "one fallback per skipped path"
    for decided in ("pass_agent", "decision=refuse", "decision=pass"):
        assert decided not in run, f"the step shell decides: {decided}"


def test_the_accept_step_names_only_the_contexts_it_needs() -> None:
    step = _accept()
    run_contexts = set(re.findall(r"\$\{\{\s*(.+?)\s*\}\}", str(step["run"])))
    env_contexts = {
        c
        for value in step["env"].values()
        for c in re.findall(r"\$\{\{\s*(.+?)\s*\}\}", str(value))
    }
    assert run_contexts | env_contexts == ACCEPT_CONTEXTS, sorted(
        run_contexts | env_contexts
    )
    assert step["env"]["ACCEPT_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_the_ci_overlay_that_holds_the_nodes_deployment_facts_is_fetched_and_rooted() -> (
    None
):
    roots = str(_accept()["env"]["ONEX_SKILL_OVERLAY_ROOTS"])
    assert roots == "${{ github.workspace }}/../ci-overlay/config/onex/overlays"
    fetch = step_named(REVIEW_JOB, "Fetch the CI overlay")
    sparse = [
        line.strip() for line in str(fetch["with"]["sparse-checkout"]).splitlines()
    ]
    assert f"config/onex/overlays/{OVERLAY_ID}" in sparse, sparse
    assert "config/onex/overlays/node_review_voters_overlay_compute" in sparse, sparse


def test_the_accept_step_sits_between_the_review_and_the_enforcement_step() -> None:
    names = [str(s.get("name", "")) for s in _steps()]
    review = names.index(step_named(REVIEW_JOB, "Run adversarial review")["name"])
    accept = names.index(str(_accept()["name"]))
    enforce = names.index(
        step_named(REVIEW_JOB, "Enforce hostile-review verdict")["name"]
    )
    assert review < accept < enforce


def test_the_enforcement_step_changes_only_its_condition() -> None:
    step = step_named(REVIEW_JOB, "Enforce hostile-review verdict")
    assert set(step) == {"name", "if", "run"}, sorted(step)
    assert step["if"] == ENFORCE_IF
    digest = hashlib.sha256(str(step["run"]).encode()).hexdigest()
    assert digest == ENFORCE_RUN_SHA256, "the run: text of the enforcement step moved"
