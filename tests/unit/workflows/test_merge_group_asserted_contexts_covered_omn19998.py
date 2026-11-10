# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every context the queue-commit rollup asserts on has a queue producer (OMN-23623).

CI Summary asserts every EXPECTED_EXTERNAL_CONTEXTS context on a queue commit,
except the one context no queue commit can carry (repo-evidence, which its own
shim job reports separately; see gate.expected_external_contexts). A context reaches a queue commit only
through a producer declaring `merge_group` (the rows of
ci_summary_external_producers.json) or through exactly one merge-group shim job
named as the context (merge-group-pr-scoped-contexts.yml). A context with
neither can never be observed on a queue commit, so every merge-queue entry
waits until the queue's check timeout and is ejected. These tests read this
repository's own files and the gate module, and fail while any asserted
queue-commit context has no producer.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
SHIM = "merge-group-pr-scoped-contexts.yml"
pytestmark = pytest.mark.unit

sys.path.insert(0, str(ROOT / "scripts" / "ci"))
gate = importlib.import_module("ci_summary_gate")


def _load(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


def _producer_events() -> dict[str, Any]:
    rows = json.loads(
        (ROOT / "scripts" / "ci" / "ci_summary_external_producers.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(rows, dict)
    return rows


def _shim_job_names() -> list[Any]:
    return [job.get("name") for job in _load(SHIM)["jobs"].values()]


def _queue_coverage() -> tuple[set[str], list[str]]:
    """Queue-commit contexts with a producer, and the asserted set with none.

    A context is covered when its producers row declares the `merge_group`
    trigger, or when exactly one shim job is named as the context. YAML loading resolves an
    escaped name to its exact context spelling, so loaded names compare
    directly against the asserted tuple.
    """

    events = _producer_events()
    names = _shim_job_names()
    covered: set[str] = set()
    uncovered: list[str] = []
    for context in gate.expected_external_contexts("merge_group"):
        row = events.get(context, {})
        declared = "merge_group" in row.get("events", [])
        shimmed = names.count(context) == 1
        if declared or shimmed:
            covered.add(context)
        else:
            uncovered.append(context)
    return covered, uncovered


def _queue_check_runs(covered: set[str]) -> list[dict[str, Any]]:
    """One successful check-run per covered context, as a queue commit carries."""

    return [
        {
            "name": context,
            "status": "completed",
            "conclusion": "success",
            "id": index,
        }
        for index, context in enumerate(sorted(covered), start=1)
    ]


def test_every_context_the_rollup_asserts_on_a_queue_commit_has_a_producer() -> None:
    _, uncovered = _queue_coverage()
    assert uncovered == [], (
        "asserted queue-commit contexts with no merge_group producer trigger "
        f"and no merge-group shim job: {uncovered}"
    )


def test_rollup_decision_is_success_for_the_check_runs_a_queue_commit_carries() -> None:
    covered, _ = _queue_coverage()
    code, report = gate.evaluate_external(
        _queue_check_runs(covered),
        expected=gate.expected_external_contexts("merge_group"),
    )
    assert code == gate.EXIT_SUCCESS, report


def test_rollup_stays_pending_when_a_producer_is_missing() -> None:
    covered, _ = _queue_coverage()
    check_runs = [
        row for row in _queue_check_runs(covered) if row["name"] != "reviewer-eval"
    ]
    code, report = gate.evaluate_external(
        check_runs,
        expected=gate.expected_external_contexts("merge_group"),
    )
    assert code == gate.EXIT_PENDING, report
    assert "reviewer-eval" in report


def test_pull_request_assertion_is_not_widened() -> None:
    assert "reviewer-eval" in gate.expected_external_contexts("pull_request")
    assert (
        gate.expected_external_contexts("pull_request")
        == gate.EXPECTED_EXTERNAL_CONTEXTS
    )
    assert set(gate.expected_external_contexts("merge_group")) == (
        set(gate.EXPECTED_EXTERNAL_CONTEXTS) - {"repo-evidence / dod-verify"}
    )


def test_reviewer_eval_job_is_one_job_of_the_merge_group_workflow() -> None:
    assert _shim_job_names().count("reviewer-eval") == 1
    # The workflow's `on:` key loads as the Python key True.
    assert set(_load(SHIM)[True]) == {"merge_group"}
