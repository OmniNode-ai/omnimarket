# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""def-B canonical-shape flip proof for node_occ_companion_compute (OMN-14835).

Class-B Tier-1 canonical-shape fan-out (parent epic OMN-14355). Both contract-declared
handlers were flipped from the non-canonical multi-positional
``handle(self, correlation_id, request)`` (def-A) to the canonical single-payload
``handle(self, request) -> response`` (def-B). The business logic
(``compute_companion_plan`` / ``verify_companion_attestation`` and every helper) is
preserved byte-for-byte — this is a pure adapter-boundary signature change
(OMN-14781 hand-flip path).

This module proves the flip against the REAL runtime dispatch surface:

  * ``test_handle_is_single_payload_defb`` — inspects the live ``handle`` signature of
    both handlers: exactly one non-self positional parameter (``request``). On the
    pre-flip def-A tree this parameter list is ``[correlation_id, request]`` (two
    positionals), so this assertion is RED before the flip and GREEN after.
  * ``test_defb_dispatch_green_and_equivalent`` — drives each corpus request through the
    canonical ``LocalRuntimeBusAdapter`` over an in-memory bus. The def-B
    ``handle(request)`` binds the validated payload and the terminal
    ``ModelOccCompanionPlan`` is published on the declared completed topic (GREEN);
    the dispatched plan is byte-equal to a direct ``compute_companion_plan(request)``
    call (behavior equivalence). On the def-A tree the adapter's single-positional
    call raises (the second ``request`` param is unbound), NO terminal event is
    published, and this test is RED.
  * ``test_attestation_defb_dispatch_green`` — the twin attestation handler dispatches
    under def-B too.

The corpus (``build_corpus``) is the SELECTED input set bound by ``input_hash`` into both
the adequacy receipt and the hand-flip proof under ``scripts/ci/adequacy_receipts/``.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_attestation import (
    HandlerOccCompanionAttestation,
    verify_companion_attestation,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    HandlerOccCompanionCompute,
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_attestation_result import (
    ModelOccAttestationRequest,
    ModelOccAttestationResult,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.occ-companion-compute-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.occ-companion-compute-completed.v1"


def _probe(stdout: str = '{"number":321,"state":"OPEN"}') -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout=stdout,
        exit_code=0,
    )


def _request(**overrides: Any) -> ModelOccCompanionRequest:
    base: dict[str, Any] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "pr_state": "open",
        "pr_head_ref": "feature-branch",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)


def build_corpus() -> list[ModelOccCompanionRequest]:
    """Deterministic input corpus exercising the handler's decision branches.

    This is the canonical, frozen selected-input set. It is imported by the
    adequacy/hand-flip minting script so the receipt's ``selected_input_hashes`` and
    the hand-flip proof's ``parity.selected_input_hashes`` are identical to what these
    parity tests drive.
    """
    return [
        # 1. Fresh path (open PR, ticket cited, non-trivial changed_files) -> real
        #    companion files (contract + downstream receipt).
        _request(
            pr_number=321,
            changed_files=("README.md", "docs/notes.md"),
            diff_total_lines=40,
        ),
        # 2. Suppressed: closed product PR -> no-op plan (F-17).
        _request(pr_number=322, pr_state="closed"),
        # 3. Suppressed: draft product PR -> no-op plan (F-17).
        _request(pr_number=323, pr_is_draft=True),
        # 4. Trivial-infra fast-path -> fast_path plan (companion skipped).
        _request(
            pr_number=324,
            changed_files=("deploy/service.yaml",),
            diff_total_lines=2,
        ),
        # 5. Private product repo, fresh path -> receipt-local check_values (F-16).
        _request(
            pr_number=325,
            product_repo_private=True,
            changed_files=("README.md",),
            diff_total_lines=10,
        ),
        # 6. Deploy-sensitive runtime path, fresh -> deploy-assessment receipt (F-05).
        _request(
            pr_number=326,
            changed_files=("src/omnimarket/nodes/node_demo/handlers/handler_demo.py",),
            diff_total_lines=30,
        ),
        # 7. No ticket cited anywhere -> no-op plan.
        _request(
            pr_number=327,
            pr_title="chore: tidy up",
            pr_body="no ticket here",
        ),
    ]


async def _drive(
    bus: EventBusInmemory, request: ModelOccCompanionRequest
) -> ModelOccCompanionPlan | None:
    """Publish a request onto the command topic; return the terminal plan (or None).

    None means the handler raised inside dispatch and no terminal event was published
    — the def-A (pre-flip) RED signal.
    """
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerOccCompanionCompute(),
        handler_name="occ-companion-compute",
        input_model_cls=ModelOccCompanionRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND, on_message=adapter.on_message, group_id="occ-defb-test"
    )
    await bus.publish(TOPIC_COMMAND, None, request.model_dump_json().encode("utf-8"))
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    if not completed:
        return None
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    return ModelOccCompanionPlan.model_validate(json.loads(completed[-1].value))


@pytest.mark.unit
def test_handle_is_single_payload_defb() -> None:
    """Both handlers expose the canonical def-B single-payload ``handle`` signature."""
    for handler_cls in (HandlerOccCompanionCompute, HandlerOccCompanionAttestation):
        sig = inspect.signature(handler_cls.handle)
        positional = [
            name
            for name, p in sig.parameters.items()
            if name != "self"
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert positional == ["request"], (
            f"{handler_cls.__name__}.handle must be canonical def-B "
            f"handle(self, request); got positional params {positional}"
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_obj", build_corpus(), ids=lambda r: f"pr{r.pr_number}"
)
async def test_defb_dispatch_green_and_equivalent(
    request_obj: ModelOccCompanionRequest,
) -> None:
    """def-B handle dispatches over the real adapter and equals the pure function."""
    bus = EventBusInmemory(environment="unit-test", group="occ-defb")
    await bus.start()
    try:
        dispatched = await _drive(bus, request_obj)
    finally:
        await bus.close()

    assert dispatched is not None, (
        "def-B handle(request) must dispatch and publish a terminal plan; a None "
        "result is the def-A (multi-positional) RED signal"
    )
    direct = compute_companion_plan(request_obj)
    assert dispatched.model_dump(mode="json") == direct.model_dump(mode="json"), (
        "dispatched terminal plan must be byte-equal to compute_companion_plan "
        "(behavior equivalence across the adapter boundary)"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attestation_defb_dispatch_green() -> None:
    """The twin attestation handler dispatches under def-B and returns a verdict."""
    request = build_corpus()[0]
    plan = compute_companion_plan(request)
    attest_request = ModelOccAttestationRequest(
        observed_files=plan.companion_files, expected=request
    )

    bus = EventBusInmemory(environment="unit-test", group="occ-attest-defb")
    await bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=HandlerOccCompanionAttestation(),
            handler_name="occ-companion-attestation",
            input_model_cls=ModelOccAttestationRequest,
            output_topic="onex.evt.omnimarket.occ-companion-attest-completed.v1",
            bus=bus,
        )
        await bus.subscribe(
            "onex.cmd.omnimarket.occ-companion-attest-requested.v1",
            on_message=adapter.on_message,
            group_id="occ-attest-defb-test",
        )
        await bus.publish(
            "onex.cmd.omnimarket.occ-companion-attest-requested.v1",
            None,
            attest_request.model_dump_json().encode("utf-8"),
        )
        history = await bus.get_event_history(
            topic="onex.evt.omnimarket.occ-companion-attest-completed.v1"
        )
    finally:
        await bus.close()

    assert len(history) == 1, "attestation handle(request) must dispatch under def-B"
    result = ModelOccAttestationResult.model_validate(json.loads(history[-1].value))
    # Byte-reproducible observed files -> the oracle accepts (equivalence with the
    # direct verify_companion_attestation call).
    direct = verify_companion_attestation(plan.companion_files, request)
    assert result.accepted is True
    assert result.model_dump(mode="json") == direct.model_dump(mode="json")
