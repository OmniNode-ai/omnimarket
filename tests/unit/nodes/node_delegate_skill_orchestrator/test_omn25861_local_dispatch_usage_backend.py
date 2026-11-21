# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25861: the local delegate path books each usage call with the alias it was reached through.

The local producer built one usage call per attempt from ``_AttemptUsage``, which carried the model but no backend,
so no usage row could say which alias served its model. Each attempt now states one (the spec's D1, the rule of
delegation_model_routing_view_v4.sql): the backend_id of a customer's own BYOK route, such as byok-reflection, or
the model itself when the route was the house's own, which the row counts as direct and lists nothing for.

AC1's falsifier on the real producer: two runs on one model, one through byok-reflection and one direct, fail unless
the model's row serves ["byok-reflection"].

Failure modes:
P1  a call reached through a BYOK route does not carry that route's id, so the row serves no backends (AC1);
P2  a call on the house's own route is listed as a backend, so no real call is ever direct;
P3  an escalated run books every call to the route that answered last.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.handlers.routing import delegation_backend_resolution
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_usage_by_model_day import (
    _BACKENDS,
    _ladder_run,
    _local_backend,  # noqa: F401  (fixture, used by name)
    _MeteredTierEffect,
    _passing_named_test_runner,
    _rows,
)

_MODEL = "Qwen3.6-35B-A3B"


def _dispatch_through(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, backend_id: str
) -> dict[str, Any]:
    """One completed run on _MODEL, routed through the catalogue backend ``backend_id``."""
    backends = [dict(_BACKENDS[0], backend_id=backend_id)]
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            named_tests=ModelNamedTestSpec(
                workspace_root=str(db_path.parent),
                answer_path="reverse_impl.py",
                test_ids=("test_reverse.py::test_reverse",),
            ),
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


@pytest.mark.usefixtures("_local_backend")
def test_a_model_reached_through_byok_reflection_serves_it_from_the_real_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 on the local delegate path: one run direct, one through byok-reflection, on one model."""
    db_path = tmp_path / "delegation.sqlite"
    assert (
        _dispatch_through(db_path, monkeypatch, "local-coder")["status"] == "completed"
    )
    assert (
        _dispatch_through(db_path, monkeypatch, "byok-reflection")["status"]
        == "completed"
    )

    calls = sorted(
        (
            c["backend"]
            for c in _rows(db_path, "SELECT backend FROM usage_by_model_day_calls")
        ),
    )
    # P2: the house's own route is the model itself, which the row counts as direct.
    assert calls == [_MODEL, "byok-reflection"]
    (row,) = _rows(
        db_path, "SELECT model_id, call_count, backends FROM usage_by_model_day"
    )
    assert (row["model_id"], row["call_count"]) == (_MODEL, 2)
    assert json.loads(row["backends"]) == ["byok-reflection"]


def test_an_escalated_run_on_the_house_ladder_lists_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 and P3: every rung of the house ladder is the house's own route, so each model's row is direct."""
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
        ("Qwen3.6-35B-A3B", "Qwen3.6-35B-A3B"),
        ("glm-5.2", "glm-5.2"),
    ]
    rows = {
        r["model_id"]: json.loads(r["backends"])
        for r in _rows(db_path, "SELECT model_id, backends FROM usage_by_model_day")
    }
    assert rows == {"Qwen3.6-35B-A3B": [], "glm-5.2": []}
