# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime route of node_validator_workflow_orchestrator (OMN-25055).

The runtime builds a handler with no arguments; the handler then loads the node overlays from the
overlay roots on its first request. These tests build it that way, route to it through the
contract, and read the result the runtime would publish.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_validator_workflow_orchestrator import (
    handlers as handlers_pkg,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters import two_backend
from omnimarket.nodes.node_validator_workflow_orchestrator.handlers.handler_validator_workflow_runtime import (
    HandlerValidatorWorkflowRuntime,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.handlers.validator_workflow import (
    CONTRACT,
    load_topics,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    EnumGateConclusion,
    ModelValidatorWorkflowRequest,
)

pytestmark = pytest.mark.unit

NODES = Path(handlers_pkg.__file__).resolve().parents[2]


def _roots(tmp_path: Path) -> dict[str, str]:
    """An overlay root holding both node overlays, each the package's example."""
    for node in (
        "node_validator_workflow_orchestrator",
        "node_validator_verdict_reducer",
    ):
        target = tmp_path / node
        target.mkdir()
        example = NODES / node / "overlay.example.yaml"
        (target / "overlay.yaml").write_text(
            example.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _request(pr: int = 1) -> ModelValidatorWorkflowRequest:
    return ModelValidatorWorkflowRequest(
        correlation_id="c-runtime", repo=two_backend.REPO, pr=pr, head="h", base="b"
    )


def test_runtime_handler_builds_with_no_arguments_and_reads_no_overlay() -> None:
    HandlerValidatorWorkflowRuntime(environ={})


def test_runtime_handler_without_an_overlay_refuses_naming_the_overlay() -> None:
    handler = HandlerValidatorWorkflowRuntime(environ={})
    with pytest.raises(
        NodeOverlayNotFoundError, match="node_validator_workflow_orchestrator"
    ):
        handler.handle(_request())


def test_runtime_handler_handles_a_request_from_the_overlays_and_logs_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handler = HandlerValidatorWorkflowRuntime(environ=_roots(tmp_path))
    with caplog.at_level(logging.INFO):
        result = handler.handle(_request())
    # The example overlay's reader holds no planted change: the unreadable change posts a failing gate.
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.startswith("read-failed")
    assert result.gate is not None
    assert result.gate.conclusion is EnumGateConclusion.FAILURE
    assert "node_validator_workflow_orchestrator handled" in caplog.text


def test_runtime_handler_runs_the_reducer_for_a_readable_change() -> None:
    planted = {f"{two_backend.REPO}#1": two_backend.planted_clean()}
    from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
        example_overlay as reducer_example,
    )
    from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
        example_overlay,
    )

    built = two_backend.build_workflow(
        example_overlay(), reducer_example(), {}, planted=planted
    )
    result = HandlerValidatorWorkflowRuntime(workflow=built).handle(_request())
    assert result.status != "failed"
    assert result.verdict is not None
    assert result.verdict.verdict.value == "pass"


def test_contract_routes_the_requested_topic_to_the_runtime_handler() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    topics = load_topics()
    assert contract["event_bus"]["subscribe_topics"] == [topics["workflow_requested"]]
    assert set(contract["event_bus"]["publish_topics"]) == {
        topics["workflow_completed"],
        topics["workflow_failed"],
    }
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
    (route,) = contract["handler_routing"]["handlers"]
    handler_cls = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    event_model = getattr(
        importlib.import_module(route["event_model"]["module"]),
        route["event_model"]["name"],
    )
    assert handler_cls is HandlerValidatorWorkflowRuntime
    assert event_model is ModelValidatorWorkflowRequest
    handler_cls()  # the runtime constructs it with no arguments
