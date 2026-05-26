# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave 6 COMPUTE stub nodes — contract + handler tests covering all 6 nodes.

OMN-12231: node_insights_to_plan_compute, node_plan_audit_compute,
node_resume_session_compute, node_rewind_compute, node_rrh_compute,
node_skill_functional_audit_compute.

All nodes are COMPUTE, pure, idempotent, node_not_implemented: true.
Each handler raises NotImplementedError (stub-ok).
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
@pytest.mark.parametrize("node_name", _WAVE6_NODES)
def test_contract_is_not_implemented(node_name: str) -> None:
    """Every Wave 6 node declares node_not_implemented: true."""
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
def test_insights_to_plan_handler_stub() -> None:
    """HandlerInsightsToPlanCompute raises NotImplementedError."""
    handler = HandlerInsightsToPlanCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(
            ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
        )


@pytest.mark.unit
def test_insights_to_plan_request_frozen() -> None:
    """ModelInsightsToPlanComputeRequest is frozen."""
    req = ModelInsightsToPlanComputeRequest(html_path="/tmp/insights.html")
    with pytest.raises(ValidationError):
        req.html_path = "/other"  # type: ignore[misc]


@pytest.mark.unit
def test_plan_audit_handler_stub() -> None:
    """HandlerPlanAuditCompute raises NotImplementedError."""
    handler = HandlerPlanAuditCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml"))


@pytest.mark.unit
def test_plan_audit_request_frozen() -> None:
    """ModelPlanAuditComputeRequest is frozen."""
    req = ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml")
    with pytest.raises(ValidationError):
        req.plan_path = "/other"  # type: ignore[misc]


@pytest.mark.unit
def test_resume_session_handler_stub() -> None:
    """HandlerResumeSessionCompute raises NotImplementedError."""
    handler = HandlerResumeSessionCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(
            ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
        )


@pytest.mark.unit
def test_resume_session_request_frozen() -> None:
    """ModelResumeSessionComputeRequest is frozen."""
    req = ModelResumeSessionComputeRequest(task_id="task-001", agent_id="agent-001")
    with pytest.raises(ValidationError):
        req.task_id = "task-002"  # type: ignore[misc]


@pytest.mark.unit
def test_rewind_handler_stub() -> None:
    """HandlerRewindCompute raises NotImplementedError."""
    handler = HandlerRewindCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(
            ModelRewindComputeRequest(
                agent_name="agent", timestamp="2026-05-25T00:00:00Z"
            )
        )


@pytest.mark.unit
def test_rewind_request_frozen() -> None:
    """ModelRewindComputeRequest is frozen."""
    req = ModelRewindComputeRequest(
        agent_name="agent", timestamp="2026-05-25T00:00:00Z"
    )
    with pytest.raises(ValidationError):
        req.agent_name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_rrh_handler_stub() -> None:
    """HandlerRrhCompute raises NotImplementedError."""
    handler = HandlerRrhCompute()
    with pytest.raises(NotImplementedError):
        handler.handle(ModelRrhComputeRequest(release_id="v1.2.3"))


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
