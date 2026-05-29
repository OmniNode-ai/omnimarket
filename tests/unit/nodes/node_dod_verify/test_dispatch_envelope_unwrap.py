# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression tests for node_dod_verify runtime dispatch (OMN-12420).

The node had never produced a terminal event. Two issues were diagnosed:

Defect 1 (the real, total blocker): the ``operation_match`` handler entries in
``contract.yaml`` declared no per-operation ``event_model``. The runtime's
``_make_dispatch_callback`` therefore took the ``event_model is None`` branch
and handed the handler the *raw* envelope dict
(``{"payload": {...}, "partition_key": None, ...}``) instead of the unwrapped
``payload``. ``ModelDodVerifyStartCommand(**envelope)`` then raised a 6-error
ValidationError, the dispatcher threw before computing anything, the Kafka
offset still advanced, and no terminal event was ever published.

Defect 2 (diagnosed as architectural, found NOT to be a defect): once the
handler returns a ``ModelDodVerifyState``, the runtime auto-wires a
``DispatchResultApplier`` for non-projection compute nodes that declare a
``terminal_event`` in their ``publish_topics`` (see
``handler_wiring._make_event_bus_subscriptions``). That applier publishes the
handler result to ``dod-verify-completed.v1``. So the terminal-event path
already exists for compute nodes — the only thing stopping it was Defect 1.

These tests assert the fixed dispatch path: the runtime dispatch callback,
built with the contract's declared ``event_model``, unwraps the envelope
payload, validates it against ``ModelDodVerifyStartCommand`` (no
ValidationError), and returns a ``ModelDispatchResult`` carrying the
``ModelDodVerifyState`` that the runtime publishes as the terminal event.
"""

from __future__ import annotations

# Path to the packaged contract that ships with the node.
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dod_verify"
    / "contract.yaml"
)


class _StubCollectorHandler(HandlerDodVerify):
    """HandlerDodVerify with a deterministic in-memory collector (no Linear I/O)."""

    @staticmethod
    def _make_collector() -> object:
        class _Collector:
            def collect(
                self, ticket_id: str, contract_path: str | None
            ) -> list[ModelEvidenceCheckResult]:
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="e1",
                        description="stub check",
                        status=EnumEvidenceCheckStatus.VERIFIED,
                        message="ok",
                    )
                ]

        return _Collector()


def _event_model_from_contract() -> ModelHandlerRef:
    """Read the event_model the runtime would resolve from the packaged contract.

    This proves the contract actually declares a per-operation event_model — the
    exact field whose absence caused Defect 1.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    assert len(handlers) == 1, (
        "node_dod_verify must declare exactly one operation_match handler; the "
        "dead runtime_sha_verify operation (only ever invoked in-process) must "
        "not be on this command topic."
    )
    event_model = handlers[0]["event_model"]
    return ModelHandlerRef(
        name=event_model["name"],
        module=event_model["module"],
    )


@pytest.mark.unit
class TestDispatchEnvelopeUnwrap:
    """OMN-12420 — runtime dispatch must unwrap the envelope and produce a result."""

    def test_contract_declares_event_model(self) -> None:
        """The contract's single handler entry declares the typed event_model.

        Without this declaration the runtime passes the raw envelope dict to the
        handler (Defect 1). The model must be ModelDodVerifyStartCommand.
        """
        event_model = _event_model_from_contract()
        assert event_model.name == "ModelDodVerifyStartCommand"
        assert event_model.module == (
            "omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command"
        )

    async def test_wrapped_envelope_dispatches_without_validation_error(self) -> None:
        """The exact envelope run-node publishes is unwrapped and dispatched.

        This is the regression for Defect 1: feed the wrapped envelope
        ``{"payload": {ticket_id, contract_path}, "correlation_id": ...}`` to the
        runtime dispatch callback built with the contract's event_model, and
        assert it returns a ModelDispatchResult (no ValidationError) carrying the
        terminal ModelDodVerifyState.
        """
        event_model = _event_model_from_contract()
        callback = _make_dispatch_callback(_StubCollectorHandler(), event_model)

        envelope = ModelEventEnvelope[object](
            payload={"ticket_id": "OMN-12420", "contract_path": None},
            correlation_id=uuid4(),
            source_tool="onex.run-node",
            target_tool="node_dod_verify",
        )

        result = await callback(envelope)

        assert isinstance(result, ModelDispatchResult), (
            f"dispatch must return ModelDispatchResult, got {type(result)!r}"
        )
        assert result.status == EnumDispatchStatus.SUCCESS
        # The runtime DispatchResultApplier publishes output_events to the
        # contract terminal_event topic — this is the terminal-event path.
        assert len(result.output_events) == 1
        state = result.output_events[0]
        assert isinstance(state, ModelDodVerifyState)
        assert state.ticket_id == "OMN-12420"
        assert state.total_checks == 1
        assert state.verified_count == 1

    def test_bare_payload_validates_against_command_model(self) -> None:
        """A bare run-node payload (no correlation_id/requested_at) must validate.

        run-node sends only ``{"ticket_id": ..., "contract_path": null}``. The
        command model must default correlation_id and requested_at so the typed
        validation in the dispatch callback succeeds.
        """
        command = ModelDodVerifyStartCommand.model_validate(
            {"ticket_id": "OMN-12420", "contract_path": None}
        )
        assert command.ticket_id == "OMN-12420"
        assert command.correlation_id is not None
        assert command.requested_at is not None
