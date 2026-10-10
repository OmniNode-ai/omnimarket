# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Omnimarket PRs reach the landing orchestrator from bus events alone (OMN-20866).

The orchestrator's per-PR start was the autobind push prompt
(``onex.cmd.omnimarket.occ-autobind.v1``), which omnimarket stopped publishing
when its change-control callers were deleted at its cut-over, so no omnimarket
PR reached it. The PR watcher's ``onex.evt.omnimarket.pr-state-observed.v1``
still carries omnimarket PRs. These tests replay real records of that topic,
read from the dev-lane broker, through the route the contract declares for it,
and follow a green, mergeable canary PR to a merge: GitHub refuses to arm a PR
it already reports clean, so the GitHub landing effect merges it at its head.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.github_landing.github_landing_requests import MERGE_AT_HEAD_MUTATION
from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers.handler_pr_landing_github import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubPrStateFact,
    ModelPrLandingGithubCompleted,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingMergedIngress,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    load_scenario,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    FixedClassifier,
    answer,
    only_request,
    passing_gate_facts,
)

pytestmark = pytest.mark.unit

_TOPIC = "onex.evt.omnimarket.pr-state-observed.v1"
_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_pr_landing_orchestrator/contract.yaml"
_RECORDS = _ROOT / "tests/fixtures/pr_landing/pr_state_observed_dev_lane.json"
_CLEAN = "arm_auto_merge_clean_merges"


def _records() -> dict[tuple[str, int], dict[str, Any]]:
    data = json.loads(_RECORDS.read_text("utf-8"))
    assert data["topic"] == _TOPIC
    return {
        (r["value"]["repo"], r["value"]["pr_number"]): r["value"]
        for r in data["records"]
    }


def _route_model() -> type[BaseModel]:
    """The event model the contract routes the watcher's topic to, as the runtime does."""
    contract = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    assert _TOPIC in contract["event_bus"]["subscribe_topics"]
    (route,) = [
        h for h in contract["handler_routing"]["handlers"] if h.get("topic") == _TOPIC
    ]
    assert route["handler"]["name"] == "HandlerPrLandingOrchestrator"
    model = getattr(
        importlib.import_module(route["event_model"]["module"]),
        route["event_model"]["name"],
    )
    assert isinstance(model, type)
    assert issubclass(model, BaseModel)
    return model


def _handler(verdict: EnumHeadCheckVerdict) -> HandlerPrLandingOrchestrator:
    """The runtime's handler (contract config, real reducer and arm gate)."""
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        classifier=FixedClassifier(verdict),
        store=InMemoryPrLandingRowStore(),
        # No hold in force and a PASS lab proof for every head (OMN-20866).
        gate_facts=passing_gate_facts(),
    )


def _states(emitted: list[BaseModel]) -> list[EnumPrLandingState]:
    return [e.to_state for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


async def test_real_watcher_records_prompt_a_read_through_the_contract_route() -> None:
    """Each dev-lane record of the canary prompts one live PR read; others none."""
    model = _route_model()
    records = _records()
    handler = _handler(EnumHeadCheckVerdict.GREEN)
    for repo, pr in (("omnimarket", 3538), ("omnimarket", 3593)):
        value = records[(repo, pr)]
        read = only_request(await handler.handle(model.model_validate(value)))
        assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
        assert read.mode is EnumPrLandingGithubMode.ENFORCE
        assert read.repository == "OmniNode-ai/omnimarket"
        assert read.pr_number == pr
    other = records[("omnibase_infra", 4801)]
    assert await handler.handle(model.model_validate(other)) == []


async def test_a_green_mergeable_canary_pr_is_merged_not_left_unarmed() -> None:
    """Watcher record, PR read, green head, enforce arm; the effect merges it."""
    model = _route_model()
    value = _records()[("omnimarket", 3538)]
    scenario = load_scenario(_CLEAN)
    assert scenario.command["head_sha"] == value["head_sha"]
    handler = _handler(EnumHeadCheckVerdict.GREEN)

    read = only_request(await handler.handle(model.model_validate(value)))
    pr_state = ModelGithubPrStateFact.model_validate(
        {
            "pr_node_id": scenario.command["pr_node_id"],
            "pr_number": value["pr_number"],
            "head_sha": value["head_sha"],
            "base_ref": "dev",
            "draft": False,
            # A ticket token, or the workflow parks the PR (the record's own
            # title is replaced with neutral text in the fixture).
            "title": f"feat(OMN-20866): canary pr {value['pr_number']}",
            "labels": (),
            "state": "open",
            "merged": False,
            "auto_merge_armed": False,
            "mergeable_state": "clean",
        }
    )
    head_read = only_request(await handler.handle(answer(read, pr_state=pr_state)))
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    emitted = await handler.handle(answer(head_read))
    assert _states(emitted) == [EnumPrLandingState.READY]
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.mode is EnumPrLandingGithubMode.ENFORCE

    # The GitHub landing effect, on GitHub's answers for a clean, unarmed PR.
    transport = FakeGithubLandingTransport.for_scenario(_CLEAN)
    result = await HandlerPrLandingGithubEffect(transport).handle(arm)
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted), result
    mutation = transport.sent[-1]
    assert mutation.body is not None
    assert mutation.body["query"] == MERGE_AT_HEAD_MUTATION
    assert mutation.body["variables"]["head"] == value["head_sha"]

    completed = ModelPrLandingGithubCompletedIngress.model_validate(result.model_dump())
    assert _states(await handler.handle(completed)) == [EnumPrLandingState.ARMED]
    merged = ModelPrLandingMergedIngress.model_validate(
        {
            "event_id": "merged-3538",
            "repo": "OmniNode-ai/omnimarket",
            "branch": "dev",
            "pr_number": value["pr_number"],
            "ticket": "OMN-20866",
            "merged_at": datetime(2026, 10, 10, 7, 30, tzinfo=UTC).isoformat(),
        }
    )
    assert _states(await handler.handle(merged)) == [EnumPrLandingState.MERGED]
