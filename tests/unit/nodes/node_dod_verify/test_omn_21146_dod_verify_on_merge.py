# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""dod_verify runs at merge time for the ticket a merged PR cites (OMN-21146).

Before this, ``node_dod_verify`` subscribed only to its start command, and the
only producers of that command were a delegation's terminal and a hand-run
``onex skill dod_verify``. A merged PR's ticket was verified only when the
scheduled closeout sweep reached it, so a ticket whose verifier could pass sat
open until a lane or the next sweep went back to it. These tests pin the wire
from ``onex.evt.github.pr-merged.v1`` to the same node: the contract routes the
merge event to a handler of this node, the runtime's own dispatch callback
unwraps the published merge payload into it, and the handler runs the existing
verification for the cited ticket under the hosted audience, or publishes
nothing when the merge cites no ticket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
    _topics_for_handler_entry,
)
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify_on_merge import (
    HandlerDodVerifyOnMerge,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    ModelDodVerifyState,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dod_verify"
    / "contract.yaml"
)
_MERGED_TOPIC = "onex.evt.github.pr-merged.v1"
_START_TOPIC = "onex.cmd.omnimarket.dod-verify-start.v1"
_AT = datetime(2026, 10, 15, 17, 7, 22, tzinfo=UTC)


class _RecordingVerifier:
    """Stands in for HandlerDodVerify: records the start command it was given."""

    def __init__(self) -> None:
        self.commands: list[ModelDodVerifyStartCommand] = []

    def handle(self, payload: ModelDodVerifyStartCommand) -> ModelDodVerifyState:
        self.commands.append(payload)
        return ModelDodVerifyState(
            correlation_id=payload.correlation_id,
            ticket_id=payload.ticket_id,
            started_at=_AT,
            completed_at=_AT,
        )


def _merge(ticket: str, *, event_id: str = "evt-1") -> ModelPrMergedEvent:
    return ModelPrMergedEvent(
        event_id=event_id,
        repo="OmniNode-ai/omnimarket",
        branch="omn-19401-some-branch",
        pr_number=3726,
        ticket=ticket,
        merged_at="2026-10-15T17:07:22Z",
    )


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _entry_for(topic: str) -> dict[str, Any]:
    handlers = _contract()["handler_routing"]["handlers"]
    matches = [h for h in handlers if h.get("topic") == topic]
    assert len(matches) == 1, f"exactly one handler entry must own {topic}"
    return matches[0]


@pytest.mark.unit
class TestDodVerifyOnMerge:
    def test_contract_subscribes_to_the_merge_event(self) -> None:
        topics = _contract()["event_bus"]["subscribe_topics"]
        assert _MERGED_TOPIC in topics
        assert _START_TOPIC in topics

    def test_contract_routes_the_merge_event_to_this_nodes_handler(self) -> None:
        entry = _entry_for(_MERGED_TOPIC)
        assert entry["handler"] == {
            "name": "HandlerDodVerifyOnMerge",
            "module": "omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify_on_merge",
        }
        assert entry["event_model"] == {
            "name": "ModelPrMergedEvent",
            "module": "omnimarket.events.github",
        }

    def test_runtime_wiring_assigns_each_topic_to_its_own_handler(self) -> None:
        manifest = discover_contracts_from_paths([_CONTRACT_PATH])
        assert manifest.errors == ()
        (contract,) = manifest.contracts
        assert contract.handler_routing is not None
        wired = {
            entry.handler.name: _topics_for_handler_entry(contract, entry)
            for entry in contract.handler_routing.handlers
        }
        assert wired == {
            "HandlerDodVerify": (_START_TOPIC,),
            "HandlerDodVerifyOnMerge": (_MERGED_TOPIC,),
        }

    def test_start_command_route_is_unchanged(self) -> None:
        entry = _entry_for(_START_TOPIC)
        assert entry["operation"] == "verify"
        assert entry["event_model"] == {
            "name": "ModelDodVerifyStartCommand",
            "module": "omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command",
        }

    def test_a_merge_citing_a_ticket_verifies_that_ticket_hosted(self) -> None:
        verifier = _RecordingVerifier()
        state = HandlerDodVerifyOnMerge(verifier=verifier).handle(_merge("OMN-19401"))

        assert state is not None
        assert state.ticket_id == "OMN-19401"
        (command,) = verifier.commands
        assert command.ticket_id == "OMN-19401"
        assert command.execution_audience is EnumDodVerifyExecutionAudience.HOSTED
        assert command.contract_path is None
        assert command.dry_run is False

    def test_a_redelivered_merge_asks_for_the_same_run(self) -> None:
        verifier = _RecordingVerifier()
        handler = HandlerDodVerifyOnMerge(verifier=verifier)
        handler.handle(_merge("OMN-19401", event_id="evt-7"))
        handler.handle(_merge("OMN-19401", event_id="evt-7"))
        handler.handle(_merge("OMN-19401", event_id="evt-8"))

        first, again, other = (c.correlation_id for c in verifier.commands)
        assert first == again
        assert first != other

    @pytest.mark.parametrize("ticket", ["", "not-a-ticket", "OMN-"])
    def test_a_merge_citing_no_ticket_publishes_nothing(self, ticket: str) -> None:
        verifier = _RecordingVerifier()
        assert HandlerDodVerifyOnMerge(verifier=verifier).handle(_merge(ticket)) is None
        assert verifier.commands == []

    async def test_runtime_dispatch_unwraps_the_published_merge_payload(self) -> None:
        """The callback the runtime builds from the contract's event_model.

        The payload is the exact shape the merge publisher emits, extra keys
        included, wrapped the way the bus delivers it.
        """
        event_model = _entry_for(_MERGED_TOPIC)["event_model"]
        assert isinstance(event_model, dict)
        verifier = _RecordingVerifier()
        callback = _make_dispatch_callback(
            HandlerDodVerifyOnMerge(verifier=verifier),
            ModelHandlerRef(name=event_model["name"], module=event_model["module"]),
        )
        envelope = ModelEventEnvelope[object](
            payload={
                "event_id": "evt-9",
                "topic": _MERGED_TOPIC,
                "repo": "OmniNode-ai/omnimarket",
                "branch": "omn-19401-some-branch",
                "pr_number": 3726,
                "ticket": "OMN-19401",
                "merged_at": "2026-10-15T17:07:22Z",
                "published_at": "2026-10-15T17:07:30Z",
            },
            correlation_id=uuid4(),
            source_tool="github.pr-merged-publisher",
            target_tool="node_dod_verify",
        )

        result = await callback(envelope)

        assert isinstance(result, ModelDispatchResult)
        assert result.status == EnumDispatchStatus.SUCCESS
        (state,) = result.output_events
        assert isinstance(state, ModelDodVerifyState)
        assert state.ticket_id == "OMN-19401"
        assert [c.ticket_id for c in verifier.commands] == ["OMN-19401"]
