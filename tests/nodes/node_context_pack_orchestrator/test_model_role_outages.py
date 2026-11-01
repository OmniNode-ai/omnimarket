# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Optional model-role outages preserve deterministic authority (OMN-21145)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor as F
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

from .conftest import FixtureSource, command

if TYPE_CHECKING:
    from omnibase_infra.runtime.auto_wiring.handler_wiring import ProtocolHandleable

pytestmark = pytest.mark.unit
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_pack_orchestrator"
)


class UnreachableRole(FixtureSource):
    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        self.requests.append(request)
        raise TimeoutError("fixture role deadline elapsed")


async def _exchange(
    handler: HandlerContextPackOrchestrator,
    binding: str,
    request: ModelContextPackCommand,
) -> ModelContextPackBuilt:
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors
    routing = manifest.contracts[0].handler_routing
    assert routing is not None
    route = routing.handlers[0]
    # The runtime adapter supports sync typed domain handlers too.
    callback = _make_dispatch_callback(
        cast("ProtocolHandleable", handler), event_model=route.event_model
    )
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    topic = contract["runtime_dispatch"]["command_topic"]
    envelope = ModelEventEnvelope[object](
        payload=request.model_dump(mode="json"),
        correlation_id=uuid4(),
        event_type=topic,
    )
    if binding == "typed-runtime":
        dispatched = await callback(envelope)
        assert dispatched is not None
        return ModelContextPackBuilt.model_validate(dispatched.output_events[0])
    terminals = contract["runtime_dispatch"]["terminal_events"]
    answers: list[ModelContextPackBuilt] = []
    bus = EventBusInmemory()
    await bus.start()

    async def receive(message: ModelEventMessage) -> None:
        answers.append(ModelContextPackBuilt.model_validate_json(message.value))

    async def dispatch(message: ModelEventMessage) -> None:
        dispatched = await callback(
            ModelEventEnvelope[object].model_validate_json(message.value)
        )
        assert dispatched is not None
        result = ModelContextPackBuilt.model_validate(dispatched.output_events[0])
        terminal = terminals[
            "success"
            if result.status is EnumContextPackOrchestratorStatus.OK
            else "failure"
        ]
        await bus.publish(terminal, key=None, value=result.model_dump_json().encode())

    try:
        for terminal in terminals.values():
            await bus.subscribe(terminal, group_id="role-result", on_message=receive)
        await bus.subscribe(topic, group_id="role-source", on_message=dispatch)
        await bus.publish(topic, key=None, value=envelope.model_dump_json().encode())
        assert len(answers) == 1
        return answers[0]
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["typed-runtime", "inmemory"])
@pytest.mark.parametrize(
    ("field", "factor"),
    [
        ("extraction_task_class", F.TRANSCRIPT_EXTRACTS),
        ("summary_task_class", F.TAIL_SUMMARY),
    ],
)
@pytest.mark.parametrize("outage", ["unbound", "unavailable", "timeout"])
async def test_optional_model_role_outage_keeps_required_authority(
    tmp_path: Path,
    promoted_context_profile_routing: None,
    binding: str,
    field: str,
    factor: F,
    outage: str,
) -> None:
    profile = {
        "model_id": "fixture-model",
        "required_factors": ["open_work"],
        "optional_factors": [factor.value],
        field: "extraction_proposer"
        if field == "extraction_task_class"
        else "context_compaction",
    }
    (tmp_path / "role.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    authority = FixtureSource(F.OPEN_WORK)
    bound: dict[F, ProtocolContextTierSource] = {F.OPEN_WORK: authority}
    if outage != "unbound":
        bound[factor] = (
            UnreachableRole(factor)
            if outage == "timeout"
            else FixtureSource(factor, EnumContextSourceStatus.UNAVAILABLE)
        )
    handler = HandlerContextPackOrchestrator(
        bound,
        profile_resolver=HandlerContextProfileResolver(tmp_path),
        implementation_identity=lambda: {"fixture": "1"},
    )
    result = await _exchange(handler, binding, command(profile_name="role"))
    assert result.status is EnumContextPackOrchestratorStatus.OK, result.errors
    tiers = {tier.factor: tier for tier in result.tier_statuses}
    assert tiers[factor].status is EnumContextSourceStatus.UNAVAILABLE
    assert not tiers[factor].required
    assert not tiers[factor].included
    assert factor not in result.factor_token_estimates
    assert tiers[F.OPEN_WORK].required
    assert tiers[F.OPEN_WORK].included
    assert authority.requests
    assert any("OMN-21110" in message.text for message in result.messages)
    assert all(f"[{factor.value}:" not in message.text for message in result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["typed-runtime", "inmemory"])
async def test_required_model_role_is_refused_before_any_source_call(
    tmp_path: Path, promoted_context_profile_routing: None, binding: str
) -> None:
    (tmp_path / "role.yaml").write_text(
        yaml.safe_dump(
            {
                "model_id": "fixture-model",
                "required_factors": ["open_work", "tail_summary"],
                "summary_task_class": "context_compaction",
            }
        ),
        encoding="utf-8",
    )
    authority = FixtureSource(F.OPEN_WORK)
    model = UnreachableRole(F.TAIL_SUMMARY)
    handler = HandlerContextPackOrchestrator(
        {F.OPEN_WORK: authority, F.TAIL_SUMMARY: model},
        profile_resolver=HandlerContextProfileResolver(tmp_path),
    )
    result = await _exchange(handler, binding, command(profile_name="role"))
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert not result.messages
    assert not result.tier_statuses
    assert not authority.requests
    assert not model.requests
