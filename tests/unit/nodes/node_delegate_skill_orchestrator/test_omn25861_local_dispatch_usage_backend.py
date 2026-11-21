# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25861: the local delegate path books each usage call with the backend it was reached through.

The local producer built one usage call per attempt from ``_AttemptUsage``, which carried the model but no backend,
so no usage row could say which backend served its model. Each attempt now carries the id of the backend the
dispatcher resolved for it, the same id the run's attempts record as ``backend_id``.

Failure modes:
P1  a usage call carries no backend, or another attempt's backend (an escalated run books every call to the backend
    that answered last);
P2  a model's row does not serve the backend its calls were reached through;
P3  the run's own attempt record and its usage calls name different backends for one attempt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_usage_by_model_day import (
    _ladder_run,
    _MeteredTierEffect,
    _rows,
)


def test_an_escalated_run_books_each_call_to_the_backend_that_served_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, result = _ladder_run(
        tmp_path,
        monkeypatch,
        _MeteredTierEffect(pass_tiers=frozenset({"cheap_cloud"})),
    )
    assert result["status"] == "completed"

    calls = _rows(
        db_path,
        "SELECT model_id, backend FROM usage_by_model_day_calls ORDER BY call_id",
    )
    assert [(c["model_id"], c["backend"]) for c in calls] == [
        ("Qwen3.6-35B-A3B", "local-coder"),
        ("glm-5.2", "cloud-glm"),
    ]
    # P3: the same backend the run's attempt record names for each attempt.
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert [(a["model_id"], a["backend_id"]) for a in attempts] == [
        (c["model_id"], c["backend"]) for c in calls
    ]

    rows = {
        r["model_id"]: json.loads(r["backends"])
        for r in _rows(db_path, "SELECT model_id, backends FROM usage_by_model_day")
    }
    assert rows == {"Qwen3.6-35B-A3B": ["local-coder"], "glm-5.2": ["cloud-glm"]}


def test_a_failed_run_on_the_full_ladder_books_three_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, result = _ladder_run(
        tmp_path, monkeypatch, _MeteredTierEffect(pass_tiers=frozenset())
    )
    assert result["status"] == "failed"
    rows = {
        r["model_id"]: json.loads(r["backends"])
        for r in _rows(db_path, "SELECT model_id, backends FROM usage_by_model_day")
    }
    assert rows == {
        "Qwen3.6-35B-A3B": ["local-coder"],
        "glm-5.2": ["cloud-glm"],
        "gemini-2.5-flash": ["cloud-gemini-2-5-flash"],
    }
