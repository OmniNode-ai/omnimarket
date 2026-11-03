# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22176: the workflow adds no decision logic -- one node call from the base commit, and one ``if:`` on the enforcement step.

The gate-accept step checks out the pull request's base commit into a worktree and runs
the node's ``python -m`` entry from there, as the finding veto does, so a pull request
can never supply the code that accepts itself. Its shell holds only that checkout, the
node call and the ``decision=unchanged`` fallbacks for a base without the node. The
"Enforce hostile-review verdict" step keeps its ``run:`` text byte for byte (pinned by
digest, taken from the default branch) and gains only the condition that skips it on
``pass_agent``. A base that does not carry the node, cannot be checked out, or a node
that does not run leaves the voters' verdict to decide, and the accept step never fails
the job by itself.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import OVERLAY_ID
from tests.ci.hostile_review_job import (
    REVIEW_JOB,
    Run,
    job,
    required_context_names,
    simulate,
    step_named,
)

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
    "secrets.LOCAL_LLM_SHARED_SECRET",
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
    assert set(step) == {"name", "id", "if", "working-directory", "env", "run"}, sorted(
        step
    )
    assert step["if"] == "always()"
    run = str(step["run"])
    assert "onex run-node" not in run
    assert 'worktree add --detach "$ACCEPT_BASE" "$ACCEPT_BASE_SHA"' in run
    calls = re.findall(rf"python -P -m {re.escape(NODE_MODULE)}\b", run)
    assert len(calls) == 1, calls
    assert (
        'PYTHONPATH="$ACCEPT_BASE/src" uv run --no-sync python -P -m ' + NODE_MODULE
    ) in run
    assert "PYTHONPATH" not in run.replace('PYTHONPATH="$ACCEPT_BASE/src"', "")
    assert "$GITHUB_WORKSPACE/src" not in run
    # The step holds the receipt key: -P keeps its working directory off sys.path as
    # well as running it outside the checkout under review (the test below).
    assert "python -m " not in run, "the node runs with python -P"
    assert "::notice::" in run, "a base without the node must skip with a notice"
    assert run.count("decision=unchanged") == 3, "one fallback per skipped path"
    for decided in ("pass_agent", "decision=refuse", "decision=pass"):
        assert decided not in run, f"the step shell decides: {decided}"


def test_the_accept_step_never_runs_in_the_checkout_under_review() -> None:
    """OMN-22177: the step holds the receipt key, so its working directory is trusted.

    ``python -m`` puts the working directory first on ``sys.path``: run in the pull
    request's checkout, a root-level ``json.py`` would import with the key in its
    environment, and ``uv run`` would build an empty environment from the pull
    request's own ``pyproject.toml``. The step runs in the omniintelligence clone the
    job synced, as the parse veto and the omnibase_infra accept step do.
    """
    step = _accept()
    assert step["working-directory"] == "../omniintelligence"
    run = str(step["run"])
    assert f"python -P -m {NODE_MODULE}" in run, (
        "-P keeps the working directory off sys.path"
    )
    assert "cd " not in run, "the working directory is the step's, not the shell's"
    assert "$GITHUB_WORKSPACE/src" not in run
    assert "--project" not in run
    assert "--directory" not in run


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
    assert (
        step["env"]["LOCAL_LLM_SHARED_SECRET"]
        == "${{ secrets.LOCAL_LLM_SHARED_SECRET }}"
    )


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


# -- the node runs from the base commit and fails closed ------------------------


def _run(
    tmp_path: Path,
    name: str,
    *,
    verdict: str,
    decision: str,
    base_has_node: bool = True,
    base_checkout_fails: bool = False,
    node_exit: int = 0,
) -> Run:
    work = tmp_path / name
    work.mkdir()
    return simulate(
        work,
        verdict=verdict,
        decision=decision,
        base_has_node=base_has_node,
        base_checkout_fails=base_checkout_fails,
        node_exit=node_exit,
    )


def _all(run: Run, *, green: bool) -> None:
    for context in required_context_names():
        assert run.green(context) is green, (context, run.exits, run.skipped)


def test_the_node_is_run_from_the_base_worktree_with_this_attempts_files(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path, "base", verdict="degraded", decision="pass_agent")
    assert len(run.node_calls) == 1
    assert run.outputs["accept"]["decision"] == "pass_agent"
    assert run.exits["accept"] == 0
    _all(run, green=True)


@pytest.mark.parametrize("verdict", ["passed", "degraded", "blocked"])
def test_a_base_without_the_node_leaves_the_voters_verdict_to_decide(
    tmp_path: Path, verdict: str
) -> None:
    run = _run(
        tmp_path,
        verdict,
        verdict=verdict,
        decision="pass_agent",
        base_has_node=False,
    )
    assert run.node_calls == [], "the pull request's own node must never run"
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=verdict == "passed")


def test_a_base_that_cannot_be_checked_out_leaves_the_voters_verdict_to_decide(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        "checkout",
        verdict="degraded",
        decision="pass_agent",
        base_checkout_fails=True,
    )
    assert run.node_calls == []
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=False)


@pytest.mark.parametrize("verdict", ["passed", "degraded"])
def test_a_node_that_does_not_run_never_fails_the_job_by_itself(
    tmp_path: Path, verdict: str
) -> None:
    run = _run(
        tmp_path,
        verdict,
        verdict=verdict,
        decision="pass_agent",
        node_exit=2,
    )
    assert len(run.node_calls) == 1
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=verdict == "passed")
