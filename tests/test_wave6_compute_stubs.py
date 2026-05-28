# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave 6 COMPUTE nodes — contract + handler tests covering all 6 nodes.

OMN-12231: node_insights_to_plan_compute, node_plan_audit_compute,
node_resume_session_compute, node_rewind_compute, node_rrh_compute,
node_skill_functional_audit_compute.

All nodes are COMPUTE, pure, and idempotent. Implemented nodes declare
node_not_implemented: false; remaining stubs declare node_not_implemented: true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

# ── imports ─────────────────────────────────────────────────────────────────
from omnimarket.nodes.node_insights_to_plan_compute.handlers.handler_insights_to_plan_compute import (
    HandlerInsightsToPlanCompute,
)
from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_request import (
    ModelInsightsToPlanComputeRequest,
)
from omnimarket.nodes.node_plan_audit_compute.handlers.handler_plan_audit_compute import (
    HandlerPlanAuditCompute,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)
from omnimarket.nodes.node_resume_session_compute.handlers.handler_resume_session_compute import (
    HandlerResumeSessionCompute,
)
from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_request import (
    ModelResumeSessionComputeRequest,
)
from omnimarket.nodes.node_rewind_compute.handlers.handler_rewind_compute import (
    HandlerRewindCompute,
)
from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_request import (
    ModelRewindComputeRequest,
)
from omnimarket.nodes.node_rrh_compute.handlers.handler_rrh_compute import (
    HandlerRrhCompute,
)
from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_request import (
    ModelRrhComputeRequest,
)
from omnimarket.nodes.node_skill_functional_audit_compute.handlers.handler_skill_functional_audit_compute import (
    HandlerSkillFunctionalAuditCompute,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)

# ── helpers ──────────────────────────────────────────────────────────────────

_NODES_ROOT = Path(__file__).parent.parent / "src" / "omnimarket" / "nodes"

_WAVE6_NODES = [
    "node_insights_to_plan_compute",
    "node_plan_audit_compute",
    "node_resume_session_compute",
    "node_rewind_compute",
    "node_rrh_compute",
    "node_skill_functional_audit_compute",
]

_WAVE6_IMPLEMENTED_NODES = [
    "node_insights_to_plan_compute",
    "node_plan_audit_compute",
    "node_resume_session_compute",
    "node_rewind_compute",
    "node_rrh_compute",
]

_WAVE6_STUB_NODES = [
    node_name for node_name in _WAVE6_NODES if node_name not in _WAVE6_IMPLEMENTED_NODES
]


def _load_contract(node_name: str) -> dict:  # type: ignore[type-arg]
    path = _NODES_ROOT / node_name / "contract.yaml"
    with open(path) as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


# ── contract tests ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _WAVE6_NODES)
def test_contract_is_compute(node_name: str) -> None:
    """Every Wave 6 node declares node_type: compute."""
    contract = _load_contract(node_name)
    assert contract["node_type"].lower() == "compute", (
        f"{node_name}: expected node_type=compute, got {contract['node_type']!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _WAVE6_IMPLEMENTED_NODES)
def test_contract_is_implemented(node_name: str) -> None:
    """Implemented Wave 6 nodes declare node_not_implemented: false."""
    contract = _load_contract(node_name)
    assert contract.get("node_not_implemented") is False, (
        f"{node_name}: node_not_implemented must be false"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _WAVE6_STUB_NODES)
def test_contract_is_not_implemented(node_name: str) -> None:
    """Remaining Wave 6 stubs declare node_not_implemented: true."""
    contract = _load_contract(node_name)
    assert contract.get("node_not_implemented") is True, (
        f"{node_name}: node_not_implemented must be true"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _WAVE6_NODES)
def test_contract_purity_and_idempotent(node_name: str) -> None:
    """Every Wave 6 node is pure and idempotent."""
    contract = _load_contract(node_name)
    desc = contract["descriptor"]
    assert desc["purity"] == "pure", f"{node_name}: expected purity=pure"
    assert desc["idempotent"] is True, f"{node_name}: expected idempotent=true"


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _WAVE6_NODES)
def test_contract_topics_follow_naming_convention(node_name: str) -> None:
    """subscribe and publish topics follow onex.{cmd|evt}.*.v1 convention."""
    contract = _load_contract(node_name)
    bus = contract.get("event_bus", {})
    for topic in bus.get("subscribe_topics", []):
        assert topic.startswith("onex.cmd."), (
            f"{node_name}: bad subscribe topic {topic!r}"
        )
        assert topic.endswith(".v1"), (
            f"{node_name}: subscribe topic must end .v1 — {topic!r}"
        )
    for topic in bus.get("publish_topics", []):
        assert topic.startswith("onex.evt."), (
            f"{node_name}: bad publish topic {topic!r}"
        )
        assert topic.endswith(".v1"), (
            f"{node_name}: publish topic must end .v1 — {topic!r}"
        )


# ── handler stub tests ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_insights_to_plan_handler_extracts_actions(tmp_path: Path) -> None:
    """HandlerInsightsToPlanCompute parses deterministic HTML inputs."""
    html_path = tmp_path / "insights.html"
    html_path.write_text(
        """
<html>
  <head><title>Feature Insights</title></head>
  <body>
    <h1>Evidence</h1>
    <p>Receipt gate is passing for the selected slice.</p>
    <ul>
      <li>Action: Owner: platform fix high priority receipt drift</li>
    </ul>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    handler = HandlerInsightsToPlanCompute()
    result = handler.handle(ModelInsightsToPlanComputeRequest(html_path=str(html_path)))

    assert result.status == "ok"
    assert result.plan["title"] == "Feature Insights"
    assert result.plan["action_count"] == 1
    assert result.action_items[0].priority == "high"
    assert result.action_items[0].owner == "platform"


@pytest.mark.unit
def test_insights_to_plan_request_frozen() -> None:
    """ModelInsightsToPlanComputeRequest is frozen."""
    req = ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
    with pytest.raises(ValidationError):
        req.html_path = "/other"  # type: ignore[misc]


@pytest.mark.unit
def test_plan_audit_handler_audits_valid_plan(tmp_path: Path) -> None:
    """HandlerPlanAuditCompute validates a minimal plan YAML."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        """
title: Wave 6 compute slice
tasks:
  - id: task-a
    title: Implement handler
  - id: task-b
    title: Add tests
    depends_on:
      - task-a
""".strip(),
        encoding="utf-8",
    )
    handler = HandlerPlanAuditCompute()
    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "ok"
    assert result.passed is True
    assert result.violations == []


@pytest.mark.unit
def test_plan_audit_request_frozen() -> None:
    """ModelPlanAuditComputeRequest is frozen."""
    req = ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml")
    with pytest.raises(ValidationError):
        req.plan_path = "/other"  # type: ignore[misc]


@pytest.mark.unit
def test_resume_session_handler_returns_not_found(tmp_path: Path) -> None:
    """HandlerResumeSessionCompute reports missing projections deterministically."""
    handler = HandlerResumeSessionCompute(state_dir=tmp_path)
    result = handler.handle(
        ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    )

    assert result.status == "not_found"
    assert result.session_state == {}


@pytest.mark.unit
def test_resume_session_request_frozen() -> None:
    """ModelResumeSessionComputeRequest is frozen."""
    req = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    with pytest.raises(ValidationError):
        req.task_id = "task-002"  # type: ignore[misc]


@pytest.mark.unit
def test_rewind_handler_returns_not_found(tmp_path: Path) -> None:
    """HandlerRewindCompute reports no events deterministically."""
    handler = HandlerRewindCompute(state_dir=tmp_path)
    result = handler.handle(
        ModelRewindComputeRequest(agent_name="agent", timestamp="2026-05-25T00:00:00Z")
    )

    assert result.status == "not_found"
    assert result.event_count == 0


@pytest.mark.unit
def test_rewind_request_frozen() -> None:
    """ModelRewindComputeRequest is frozen."""
    req = ModelRewindComputeRequest(
        agent_name="agent", timestamp="2026-05-25T00:00:00Z"
    )
    with pytest.raises(ValidationError):
        req.agent_name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_rrh_handler_validates_release_id() -> None:
    """HandlerRrhCompute validates registered deterministic checks."""
    handler = HandlerRrhCompute()
    result = handler.handle(ModelRrhComputeRequest(release_id="v1.2.3"))

    assert result.status == "ok"
    assert result.ready is True
    assert result.blocking_checks == []


@pytest.mark.unit
def test_rrh_request_frozen() -> None:
    """ModelRrhComputeRequest is frozen."""
    req = ModelRrhComputeRequest(release_id="v1.2.3")
    with pytest.raises(ValidationError):
        req.release_id = "v2.0.0"  # type: ignore[misc]


@pytest.mark.unit
def test_skill_functional_audit_handler_stub() -> None:
    """HandlerSkillFunctionalAuditCompute raises NotImplementedError."""
    handler = HandlerSkillFunctionalAuditCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(ModelSkillFunctionalAuditComputeRequest())


@pytest.mark.unit
def test_skill_functional_audit_request_frozen() -> None:
    """ModelSkillFunctionalAuditComputeRequest is frozen."""
    req = ModelSkillFunctionalAuditComputeRequest()
    with pytest.raises(ValidationError):
        req.skills_filter = ["some-skill"]  # type: ignore[misc]
