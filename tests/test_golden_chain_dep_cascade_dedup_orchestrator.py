# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_dep_cascade_dedup_orchestrator [OMN-12213].

Honest routing behaviour for a native orchestrator:
- contract marks node_not_implemented: false
- entry point loads
- typed models are strict (frozen, extra="forbid")
- handler deduplicates through an injected GitHub adapter
- contract declares the expected runtime routing surface
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.handlers.handler_dep_cascade_dedup_orchestrator import (
    HandlerDepCascadeDedupOrchestrator,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_result import (
    EnumPRAction,
    ModelDepCascadeDedupResult,
    ModelPackageGroup,
    ModelPRRecord,
)

_NODE_NAME = "node_dep_cascade_dedup_orchestrator"
_HANDLER_MODULE = "omnimarket.nodes.node_dep_cascade_dedup_orchestrator.handlers.handler_dep_cascade_dedup_orchestrator"
_HANDLER_CLASS = "HandlerDepCascadeDedupOrchestrator"
_REQUEST_MODULE = "omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request"
_REQUEST_CLASS = "ModelDepCascadeDedupRequest"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict:  # type: ignore[type-arg]
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_contract_is_implemented() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is False
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_contract_routing_surface() -> None:
    raw = _contract()

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "handler": {
                "name": _HANDLER_CLASS,
                "module": _HANDLER_MODULE,
            }
        }
    ]


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_contract_event_bus() -> None:
    raw = _contract()
    eb = raw["event_bus"]

    assert (
        eb["consumer_group"] == "omnimarket.dep_cascade_dedup_orchestrator.consume.v1"
    )
    assert "onex.cmd.omnimarket.dep-cascade-dedup-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.dep-cascade-dedup-completed.v1" in eb["publish_topics"]
    assert "onex.evt.omnimarket.dep-cascade-dedup-pr-closed.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.dep-cascade-dedup.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_terminal_event() -> None:
    raw = _contract()
    assert raw["terminal_event"] == "onex.evt.omnimarket.dep-cascade-dedup-completed.v1"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Input model (ModelDepCascadeDedupRequest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_dep_cascade_dedup_request_defaults() -> None:
    req = ModelDepCascadeDedupRequest()

    assert req.repos == ()
    assert req.dependency_type == ""
    assert req.label == "dependencies"
    assert req.dry_run is False
    assert req.close_comment == ""


@pytest.mark.unit
def test_model_dep_cascade_dedup_request_with_repos() -> None:
    req = ModelDepCascadeDedupRequest(
        repos=("OmniNode-ai/omnibase_core", "OmniNode-ai/omniclaude"),
        dependency_type="python",
        label="dependencies",
        dry_run=True,
        close_comment="Superseded — closing.",
    )

    assert req.repos == ("OmniNode-ai/omnibase_core", "OmniNode-ai/omniclaude")
    assert req.dependency_type == "python"
    assert req.dry_run is True
    assert req.close_comment == "Superseded — closing."


@pytest.mark.unit
def test_model_dep_cascade_dedup_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelDepCascadeDedupRequest(unexpected_field=True)  # type: ignore[call-arg]


@pytest.mark.unit
def test_model_dep_cascade_dedup_request_is_frozen() -> None:
    req = ModelDepCascadeDedupRequest()

    with pytest.raises(ValidationError):
        req.dry_run = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_dep_cascade_dedup_result_minimal() -> None:
    result = ModelDepCascadeDedupResult()

    assert result.repos_scanned == 0
    assert result.groups_found == 0
    assert result.prs_closed == 0
    assert result.prs_kept == 0
    assert result.prs_skipped == 0
    assert result.dry_run is False
    assert result.package_groups == ()
    assert result.pr_records == ()


@pytest.mark.unit
def test_model_dep_cascade_dedup_result_populated() -> None:
    group = ModelPackageGroup(
        repo="OmniNode-ai/omnibase_core",
        package="pydantic",
        keeper_pr_number=45,
        superseded_pr_numbers=(42, 43),
    )
    record_closed = ModelPRRecord(
        repo="OmniNode-ai/omnibase_core",
        pr_number=42,
        package="pydantic",
        target_version="2.9.1",
        action=EnumPRAction.CLOSED,
        superseded_by=45,
        reason="Superseded by #45 targeting pydantic@2.9.3",
    )
    record_kept = ModelPRRecord(
        repo="OmniNode-ai/omnibase_core",
        pr_number=45,
        package="pydantic",
        target_version="2.9.3",
        action=EnumPRAction.KEPT,
    )

    result = ModelDepCascadeDedupResult(
        repos_scanned=5,
        groups_found=1,
        prs_closed=1,
        prs_kept=1,
        dry_run=False,
        package_groups=(group,),
        pr_records=(record_closed, record_kept),
    )

    assert result.repos_scanned == 5
    assert result.groups_found == 1
    assert result.prs_closed == 1
    assert result.prs_kept == 1
    assert len(result.package_groups) == 1
    assert len(result.pr_records) == 2
    assert result.package_groups[0].keeper_pr_number == 45
    assert result.package_groups[0].superseded_pr_numbers == (42, 43)


@pytest.mark.unit
def test_model_dep_cascade_dedup_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelDepCascadeDedupResult(bogus_field=True)  # type: ignore[call-arg]


@pytest.mark.unit
def test_model_pr_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelPRRecord(
            repo="OmniNode-ai/omnibase_core",
            pr_number=1,
            package="pydantic",
            action=EnumPRAction.KEPT,
            bogus=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_package_group_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelPackageGroup(
            repo="OmniNode-ai/omnibase_core",
            package="pydantic",
            bogus=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_enum_pr_action_values() -> None:
    assert EnumPRAction.CLOSED == "closed"
    assert EnumPRAction.KEPT == "kept"
    assert EnumPRAction.SKIPPED == "skipped"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dep_cascade_dedup_orchestrator_handler_dedups_and_closes() -> None:
    adapter = _Adapter(
        {
            "OmniNode-ai/omnibase_core": [
                {"number": 42, "title": "Bump pydantic from 2.9.0 to 2.9.1"},
                {"number": 45, "title": "Bump pydantic from 2.9.0 to 2.9.3"},
            ]
        }
    )
    handler = HandlerDepCascadeDedupOrchestrator(adapter=adapter)
    request = ModelDepCascadeDedupRequest(
        repos=("OmniNode-ai/omnibase_core",),
        dry_run=False,
    )

    result = handler.handle(request)

    assert result.groups_found == 1
    assert result.prs_closed == 1
    assert result.prs_kept == 1
    assert adapter.closed[0][1] == 42
    assert "Superseded by #45" in adapter.closed[0][2]


class _Adapter:
    def __init__(self, prs_by_repo: dict[str, list[dict[str, object]]]) -> None:
        self._prs_by_repo = prs_by_repo
        self.closed: list[tuple[str, int, str]] = []

    def list_repos(self) -> tuple[str, ...]:
        return tuple(sorted(self._prs_by_repo))

    def list_dependency_prs(
        self, repo: str, *, label: str, dependency_type: str
    ) -> list[dict[str, object]]:
        assert label == "dependencies"
        assert dependency_type == ""
        return list(self._prs_by_repo.get(repo, []))

    def close_pr(self, repo: str, pr_number: int, comment: str) -> None:
        self.closed.append((repo, pr_number, comment))
