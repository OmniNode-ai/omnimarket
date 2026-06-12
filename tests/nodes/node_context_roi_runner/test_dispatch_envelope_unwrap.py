# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Real-dispatch-path regression for node_context_roi_runner (OMN-13003).

The runner had never received a single message on
``onex.cmd.omnimarket.context-roi-run-requested.v1`` (high-watermark 0). The
first live run-request crashed the stability-test dispatcher:

    Dispatcher 'dispatcher.auto.node_context_roi_runner.HandlerContextRoiRunner'
    failed: AttributeError: 'dict' object has no attribute 'tasks'

Root cause (CONTRACT defect, identical class to OMN-12420 node_dod_verify): the
``handler_routing.handlers[0]`` entry in ``contract.yaml`` declared only
``handler: {name, module}`` and OMITTED the ``event_model`` block. The runtime's
``_make_dispatch_callback`` therefore took the ``event_model is None`` branch
(handler_wiring.py L422) and handed the handler the *raw* materialized dispatch
envelope (a dict) instead of a validated ``ModelContextRoiRunRequest``.
``HandlerContextRoiRunner.handle`` then did ``for task in request.tasks`` on a
dict and raised ``AttributeError``.

The node's golden-chain / unit tests construct ``ModelContextRoiRunRequest(...)``
directly and call ``handle()`` — they never exercise the auto-wiring deserialize
step, which is why the defect shipped green (the live-dispatch-vs-handler-
isolation gap that memory feedback_real_dispatch_path_tests predicts).

These tests go through the REAL runtime dispatch callback built from the
contract's declared ``event_model``, replaying the exact wrapped envelope the
publisher sends. They fail before the contract fix and pass after.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef

from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)


def _event_model_from_contract() -> ModelHandlerRef:
    """Resolve the event_model the runtime would build from the packaged contract.

    Proves the contract's single handler entry declares the typed event_model —
    the exact field whose absence caused the live crash.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    assert len(handlers) == 1, (
        "node_context_roi_runner must declare exactly one handler entry"
    )
    event_model = handlers[0]["event_model"]
    return ModelHandlerRef(name=event_model["name"], module=event_model["module"])


def _run_request_payload() -> dict[str, object]:
    """The bare run-request payload the experiment publisher sends (OFF arm shape)."""
    return {
        "run_id": "omn-13003-dispatch-probe",
        "tasks": [
            {
                "task_id": "invoice_reconcile",
                "task_description": "generate a minimal compute node",
                "optional_factors": ["golden_chain", "exemplar"],
            }
        ],
        "arms": [
            {"label": "off", "factor_subset": []},
            {"label": "golden_exemplar", "factor_subset": ["golden_chain", "exemplar"]},
        ],
        "trials_per_cell": 1,
        "max_attempts": 1,
        "arm_order_seed": 42,
        "generation_timeout_seconds": 120.0,
        "artifact_content_map": {
            "golden_chain": "<golden chain text>",
            "exemplar": "<exemplar text>",
        },
    }


@pytest.mark.unit
class TestRunnerDispatchEnvelopeUnwrap:
    """OMN-13003 — runtime dispatch must deserialize the typed run-request."""

    def test_contract_declares_event_model(self) -> None:
        """The handler entry declares ModelContextRoiRunRequest as its event_model.

        Without this, the runtime passes the raw envelope dict to the handler and
        ``for task in request.tasks`` raises AttributeError.
        """
        event_model = _event_model_from_contract()
        assert event_model.name == "ModelContextRoiRunRequest"
        assert event_model.module == (
            "omnimarket.nodes.node_context_roi_runner.models."
            "model_context_roi_run_request"
        )

    async def test_wrapped_envelope_dispatches_without_attribute_error(self) -> None:
        """Replay the exact wrapped envelope through the real dispatch callback.

        This is the regression for the live crash: feed the wrapped envelope
        ``{"payload": {run_id, tasks, arms, ...}}`` to the dispatch callback built
        with the contract's event_model and assert it returns a
        ModelDispatchResult (no AttributeError, no raw-dict handoff).
        """
        event_model = _event_model_from_contract()
        # No-op publisher/consumer: every trial times out and records a
        # GENERATION-stage row, but the dispatch + typed deserialize path — the
        # thing that crashed live — is fully exercised.
        callback = _make_dispatch_callback(HandlerContextRoiRunner(), event_model)

        envelope = ModelEventEnvelope[object](
            payload=_run_request_payload(),
            correlation_id=uuid4(),
            source_tool="exp-runner-probe",
            target_tool="node_context_roi_runner",
        )

        result = await callback(envelope)

        assert isinstance(result, ModelDispatchResult), (
            f"dispatch must return ModelDispatchResult, got {type(result)!r}"
        )
        assert result.status == EnumDispatchStatus.SUCCESS
