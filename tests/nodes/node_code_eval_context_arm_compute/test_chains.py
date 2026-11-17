# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23629: the context-arm contract, golden chain and error chain over the in-memory bus.

Set OMN23629_TEST_BUS=kafka to run these cases on a separately configured Kafka binding;
the default is the in-memory binding. A missing Kafka configuration fails the cases.

Golden chain: SELECT over the bus names the arm's profile, the real context-pack
orchestrator builds the pack from fixture sources at the task's base revision, and JUDGE
over the bus accepts it. Error chain: an unavailable exemplar tier, a profile the
resolver does not know and a malformed request each end in a typed decision, never a
quietly accepted pack.
"""

from __future__ import annotations

import importlib
import json
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_code_eval_context_arm_compute as node_package
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackCommand,
)
from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    EnumContextArmDecision,
    EnumContextArmReason,
    ModelCodeEvalContextArmRequest,
    ModelCodeEvalContextArmResult,
)
from tests.nodes.node_context_pack_orchestrator.conftest import (
    FixtureSource,
    access,
    orchestrator,
    sources,
)
from tests.runtime_local_compat import RuntimeLocal

from .helpers import BASE_COMMIT_TIME, EXEMPLAR_SHA, request

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.code-eval-context-arm-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.code-eval-context-arm-decided.v1"
EXEMPLAR_SOURCE_TEXT = (
    "feat: x (#1)\n"
    "merged 2026-10-01T00:00:00+00:00 as aaaaaaaaaaaa on origin/dev\n"
    "files (1):\n"
    "A\tf"
)
INVARIANTS_SOURCE_TEXT = "Invariant: every node is contract, node and handler."
SHAPES_SOURCE_TEXT = "Canonical shape: a request in, a typed result out."


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def _pack_sources(**by_name: FixtureSource) -> dict[EnumContextFactor, FixtureSource]:
    """Fixture sources bound only for the factors the pack arms pin."""
    return sources(factors=(), **by_name)


def _resolved_pack_sources() -> dict[EnumContextFactor, FixtureSource]:
    return _pack_sources(
        invariants=FixtureSource(
            EnumContextFactor.INVARIANTS, text=INVARIANTS_SOURCE_TEXT
        ),
        canonical_shapes=FixtureSource(
            EnumContextFactor.CANONICAL_SHAPES, text=SHAPES_SOURCE_TEXT
        ),
        exemplar=FixtureSource(EnumContextFactor.EXEMPLAR, text=EXEMPLAR_SOURCE_TEXT),
    )


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeCodeEvalContextArmCompute, handler_type)
    for side, model in (
        ("input_model", ModelCodeEvalContextArmRequest),
        ("output_model", ModelCodeEvalContextArmResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_code_eval_context_arm_compute"].load() is node_package


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    (tmp_path / name).mkdir()
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={
            "event_bus": os.environ.get("OMN23629_TEST_BUS", "inmemory")
        },
        timeout=10,
    )
    await runtime.run_async()
    return runtime


async def _decide(tmp_path: Path, name: str, req: ModelCodeEvalContextArmRequest):
    runtime = await _run(tmp_path, req.model_dump(mode="json"), name)
    result = runtime.handler_result
    assert isinstance(result, ModelCodeEvalContextArmResult), result
    return result


def _build_pack(pack_request, bound: dict[EnumContextFactor, FixtureSource]):
    return orchestrator(bound).handle(
        ModelContextPackCommand(request=pack_request, access=access())
    )


@pytest.mark.asyncio
async def test_golden_chain_exemplar_arm_selects_builds_and_judges(
    tmp_path: Path,
) -> None:
    selected = await _decide(
        tmp_path, "select-exemplar", request("select", "task_plus_exemplar")
    )
    assert selected.decision is EnumContextArmDecision.REQUEST_PACK
    assert selected.pack_request is not None
    assert selected.pack_request.profile_name == "delegation_payload_exemplar"

    exemplar_source = FixtureSource(
        EnumContextFactor.EXEMPLAR, text=EXEMPLAR_SOURCE_TEXT
    )
    pack = _build_pack(selected.pack_request, _pack_sources(exemplar=exemplar_source))
    assert pack.status is EnumContextPackOrchestratorStatus.OK, pack.errors
    assert pack.tier_statuses[0].factor is EnumContextFactor.EXEMPLAR
    assert pack.tier_statuses[0].included
    assert exemplar_source.requests[0].revision == BASE_COMMIT_TIME

    judged = await _decide(
        tmp_path,
        "judge-exemplar",
        request("judge", "task_plus_exemplar", pack_answer=pack),
    )
    assert judged.decision is EnumContextArmDecision.SATISFIED
    assert judged.reason is EnumContextArmReason.OK
    assert judged.unscorable_context is False
    assert judged.included_tiers == ("exemplar",)
    assert judged.exemplar_ref == EXEMPLAR_SHA
    assert judged.final_pack_hash == pack.final_pack_hash


@pytest.mark.asyncio
async def test_golden_chain_pack_arm_selects_builds_and_judges(tmp_path: Path) -> None:
    selected = await _decide(
        tmp_path, "select-pack", request("select", "task_plus_pack")
    )
    assert selected.decision is EnumContextArmDecision.REQUEST_PACK
    assert selected.pack_request is not None
    assert selected.pack_request.profile_name == "delegation_payload_local"

    bound = _resolved_pack_sources()
    pack = _build_pack(selected.pack_request, bound)
    assert pack.status is EnumContextPackOrchestratorStatus.OK, pack.errors
    included = {tier.factor for tier in pack.tier_statuses if tier.included}
    assert included == {
        EnumContextFactor.INVARIANTS,
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.EXEMPLAR,
    }
    for source in bound.values():
        assert source.requests[0].revision == BASE_COMMIT_TIME

    judged = await _decide(
        tmp_path,
        "judge-pack",
        request("judge", "task_plus_pack", pack_answer=pack),
    )
    assert judged.decision is EnumContextArmDecision.SATISFIED
    assert judged.reason is EnumContextArmReason.OK
    assert judged.included_tiers == ("invariants", "canonical_shapes", "exemplar")
    assert judged.exemplar_ref == EXEMPLAR_SHA
    assert judged.final_pack_hash == pack.final_pack_hash


@pytest.mark.asyncio
async def test_error_chain_an_unavailable_exemplar_tier_is_never_satisfied(
    tmp_path: Path,
) -> None:
    selected = await _decide(
        tmp_path, "select-unavailable", request("select", "task_plus_pack")
    )
    assert selected.pack_request is not None
    bound = _resolved_pack_sources()
    bound[EnumContextFactor.EXEMPLAR] = FixtureSource(
        EnumContextFactor.EXEMPLAR, status=EnumContextSourceStatus.UNAVAILABLE
    )
    pack = _build_pack(selected.pack_request, bound)
    assert pack.status is EnumContextPackOrchestratorStatus.OK, pack.errors
    exemplar_tier = next(
        tier for tier in pack.tier_statuses if tier.factor is EnumContextFactor.EXEMPLAR
    )
    assert exemplar_tier.included is False

    judged = await _decide(
        tmp_path,
        "judge-unavailable",
        request("judge", "task_plus_pack", pack_answer=pack),
    )
    assert judged.decision is EnumContextArmDecision.NOT_SATISFIED
    assert judged.reason is EnumContextArmReason.REQUIRED_TIER_MISSING
    assert judged.unscorable_context is True
    assert "exemplar" in judged.unresolved_tiers


@pytest.mark.asyncio
async def test_error_chain_an_unknown_profile_fails_the_pack_and_the_judge(
    tmp_path: Path,
) -> None:
    selected = await _decide(
        tmp_path, "select-unknown-profile", request("select", "task_plus_exemplar")
    )
    assert selected.pack_request is not None
    unknown = selected.pack_request.model_copy(
        update={"profile_name": "omn23629_no_such_profile"}
    )
    pack = _build_pack(
        unknown,
        _pack_sources(
            exemplar=FixtureSource(
                EnumContextFactor.EXEMPLAR, text=EXEMPLAR_SOURCE_TEXT
            )
        ),
    )
    assert pack.status is EnumContextPackOrchestratorStatus.FAILED
    assert pack.failure_class is EnumContextPackFailure.PROFILE_NOT_FOUND

    judged = await _decide(
        tmp_path,
        "judge-unknown-profile",
        request("judge", "task_plus_exemplar", pack_answer=pack),
    )
    assert judged.decision is EnumContextArmDecision.NOT_SATISFIED
    assert judged.reason is EnumContextArmReason.PACK_FAILED
    assert judged.unscorable_context is True


@pytest.mark.asyncio
async def test_error_chain_a_malformed_request_fails_without_a_result(
    tmp_path: Path,
) -> None:
    payload = request("select", "task_plus_exemplar").model_dump(mode="json")
    payload["correlation_id"] = ""
    input_path = tmp_path / "bad-request.json"
    input_path.write_text(json.dumps(payload))
    (tmp_path / "bad-request").mkdir()
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "bad-request",
        backend_overrides={
            "event_bus": os.environ.get("OMN23629_TEST_BUS", "inmemory")
        },
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
