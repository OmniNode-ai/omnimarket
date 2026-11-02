# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22176: the workflow adds no logic -- one node call, and one ``if:`` on the enforcement step.

The gate-accept step may hold an ``onex run-node`` call, ``if: always()`` and ``${{ }}``
expressions over the pull request; the "Enforce hostile-review verdict" step keeps its
``run:`` text byte for byte (pinned by digest, taken from the default branch) and gains
only the condition that skips it on ``pass_agent``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from tests.ci.hostile_review_job import REVIEW_JOB, job, step_named

pytestmark = pytest.mark.unit

#: sha256 of the enforcement step's ``run:`` text on the default branch before OMN-22176.
ENFORCE_RUN_SHA256 = "458e14487bec17542a6779907ed147440f2062d4084779f0827281bd285b9b91"
ENFORCE_IF = "always() && steps.accept.outputs.decision != 'pass_agent'"
ACCEPT_CONTEXTS = {
    "github.repository",
    "github.event.pull_request.number",
    "github.event.pull_request.head.sha",
    "runner.temp",
}


def _steps() -> list[dict[str, Any]]:
    return list(job(REVIEW_JOB)["steps"])


def _accept() -> dict[str, Any]:
    found = [s for s in _steps() if s.get("id") == "accept"]
    assert len(found) == 1, "the job must hold exactly one gate-accept step"
    return found[0]


def test_the_accept_step_is_one_node_call_under_if_always() -> None:
    step = _accept()
    assert set(step) == {"name", "id", "if", "run"}, sorted(step)
    assert step["if"] == "always()"
    run = str(step["run"]).strip()
    assert run.startswith("onex run-node node_agent_review_gate_accept_effect "), run
    assert "\n" not in run
    for fragment in (";", "&&", "||", "|", "$(", "`", ">", "<"):
        assert fragment not in re.sub(r"\$\{\{.*?\}\}", "", run), fragment
    contexts = set(re.findall(r"\$\{\{\s*(.+?)\s*\}\}", run))
    assert contexts == ACCEPT_CONTEXTS, sorted(contexts)


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
