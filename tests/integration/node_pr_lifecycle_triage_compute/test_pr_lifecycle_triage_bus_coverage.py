# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_pr_lifecycle_triage_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute). The COMPUTE handler is
dispatched through ``LocalRuntimeBusAdapter`` over ``EventBusInmemory`` (via the
``integration_event_bus`` fixture) — a command lands on the contract-declared
subscribe topic and the runtime auto-emits the classification onto the
contract-declared publish topic ``onex.evt.omnimarket.pr-lifecycle-triage-completed.v1``.

COMPUTE DoD:
  * every declared verdict class reached (GREEN / RED / CONFLICTED /
    OCC_DEPENDENCY / NEEDS_REVIEW) and asserted on the terminal-event payload;
  * every mode/flag branch of ``_classify_pr`` exercised (conflict priority,
    receipt-only OCC vs receipt-only-no-ticket RED, awaiting-approval vs
    open-threads vs pending-CI NEEDS_REVIEW variants);
  * a negative control: a known-bad (conflicting) fixture MUST classify
    CONFLICTED and MUST NOT classify GREEN.

Zero network calls: the triage handler is pure; the bus is fully in-memory.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
    HandlerPrLifecycleTriage,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_triage_category import (
    EnumPrTriageCategory,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item import (
    ModelPrInventoryItem,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_triage_input import (
    ModelPrTriageInput,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_triage_output import (
    ModelPrTriageOutput,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_pr_lifecycle_triage_compute/contract.yaml).
_SUBSCRIBE_TOPIC = "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"
_PUBLISH_TOPIC = "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1"


class _TriageBusWrapper:
    """Bridge the runtime bus (single input model) onto the triage handler's
    ``handle(correlation_id, prs)`` calling convention."""

    def __init__(self, handler: HandlerPrLifecycleTriage) -> None:
        self._handler = handler

    async def handle(self, input_model: ModelPrTriageInput) -> ModelPrTriageOutput:
        return await self._handler.handle(
            correlation_id=input_model.correlation_id,
            prs=input_model.prs,
        )


async def _run_over_bus(bus: Any, command: ModelPrTriageInput) -> ModelPrTriageOutput:
    """Publish a triage command onto the declared subscribe topic and return the
    terminal ``ModelPrTriageOutput`` parsed off the declared publish topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=_TriageBusWrapper(HandlerPrLifecycleTriage()),
        handler_name="pr-lifecycle-triage-compute",
        input_model_cls=ModelPrTriageInput,
        output_topic=_PUBLISH_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _SUBSCRIBE_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-pr-lifecycle-triage-test",
    )
    await bus.publish(
        _SUBSCRIBE_TOPIC,
        key=None,
        value=command.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_PUBLISH_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_PUBLISH_TOPIC}"
    payload = json.loads(history[-1].value)
    return ModelPrTriageOutput.model_validate(payload)


def _item(**overrides: Any) -> ModelPrInventoryItem:
    base: dict[str, Any] = {
        "pr_number": 1,
        "repo": "OmniNode-ai/omnimarket",
        "ci_status": "passing",
        "has_conflicts": False,
        "approved": True,
        "open_threads": 0,
    }
    base.update(overrides)
    return ModelPrInventoryItem(**base)


# (fixture item, expected verdict class, expected reason substring)
_CASES = [
    pytest.param(_item(), EnumPrTriageCategory.GREEN, "ready to merge", id="green"),
    pytest.param(
        _item(has_conflicts=True, ci_status="failing"),
        EnumPrTriageCategory.CONFLICTED,
        "merge conflicts",
        id="conflicted-priority-over-failing-ci",
    ),
    pytest.param(
        _item(ci_status="failing", approved=False, failed_check_names=("build",)),
        EnumPrTriageCategory.RED,
        "fix required before merge",
        id="red-normal-ci-failure",
    ),
    pytest.param(
        _item(
            ci_status="failing",
            approved=False,
            failed_check_names=("verify / verify",),
            ticket_ids=("OMN-13674",),
        ),
        EnumPrTriageCategory.OCC_DEPENDENCY,
        "OCC dependency",
        id="occ-dependency-receipt-only-with-ticket",
    ),
    pytest.param(
        _item(
            ci_status="failing",
            approved=False,
            failed_check_names=("verify / verify",),
        ),
        EnumPrTriageCategory.RED,
        "no ticket ID was found",
        id="red-receipt-only-no-ticket",
    ),
    pytest.param(
        _item(ci_status="pending", approved=False),
        EnumPrTriageCategory.NEEDS_REVIEW,
        "awaiting approval",
        id="needs-review-pending-no-approval",
    ),
    pytest.param(
        _item(ci_status="passing", approved=False, open_threads=0),
        EnumPrTriageCategory.NEEDS_REVIEW,
        "Awaiting approval.",
        id="needs-review-passing-no-approval",
    ),
    pytest.param(
        _item(ci_status="passing", approved=False, open_threads=2),
        EnumPrTriageCategory.NEEDS_REVIEW,
        "2 unresolved review thread",
        id="needs-review-unapproved-open-threads",
    ),
    pytest.param(
        _item(ci_status="passing", approved=True, open_threads=3),
        EnumPrTriageCategory.NEEDS_REVIEW,
        "3 unresolved review thread",
        id="needs-review-approved-open-threads",
    ),
    pytest.param(
        _item(ci_status="pending", approved=True, open_threads=0),
        EnumPrTriageCategory.NEEDS_REVIEW,
        "waiting for CI to pass",
        id="needs-review-approved-pending-ci",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("item", "expected", "reason_substr"), _CASES)
async def test_triage_verdict_over_bus(
    integration_event_bus: Any,
    item: ModelPrInventoryItem,
    expected: EnumPrTriageCategory,
    reason_substr: str,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        correlation_id = uuid4()
        output = await _run_over_bus(
            bus, ModelPrTriageInput(correlation_id=correlation_id, prs=(item,))
        )

        assert output.correlation_id == correlation_id
        assert len(output.results) == 1
        result = output.results[0]
        assert result.category == expected
        assert reason_substr.lower() in result.reason.lower(), result.reason

        # The matching per-verdict tally must be exactly 1, all others 0.
        tallies = {
            EnumPrTriageCategory.GREEN: output.total_green,
            EnumPrTriageCategory.RED: output.total_red,
            EnumPrTriageCategory.CONFLICTED: output.total_conflicted,
            EnumPrTriageCategory.OCC_DEPENDENCY: output.total_occ_dependency,
            EnumPrTriageCategory.NEEDS_REVIEW: output.total_needs_review,
        }
        assert tallies[expected] == 1, tallies
        assert sum(tallies.values()) == 1, tallies
    finally:
        await bus.close()


@pytest.mark.integration
async def test_triage_negative_control_conflicting_is_not_green(
    integration_event_bus: Any,
) -> None:
    """A known-bad conflicting fixture MUST classify CONFLICTED and MUST NOT be
    reported GREEN — the negative control against a rubber-stamp classifier."""
    bus = integration_event_bus
    await bus.start()
    try:
        # Everything that would make it GREEN, plus an injected merge conflict.
        bad = _item(
            ci_status="passing", approved=True, open_threads=0, has_conflicts=True
        )
        output = await _run_over_bus(
            bus, ModelPrTriageInput(correlation_id=uuid4(), prs=(bad,))
        )
        assert output.results[0].category == EnumPrTriageCategory.CONFLICTED
        assert output.results[0].category != EnumPrTriageCategory.GREEN
        assert output.total_conflicted == 1
        assert output.total_green == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_triage_mixed_batch_tallies_over_bus(
    integration_event_bus: Any,
) -> None:
    """A heterogeneous batch reaches every verdict class in one bus round-trip
    and the terminal-event tallies sum correctly."""
    bus = integration_event_bus
    await bus.start()
    try:
        prs = (
            _item(pr_number=1),  # green
            _item(
                pr_number=2,
                ci_status="failing",
                approved=False,
                failed_check_names=("t",),
            ),
            _item(pr_number=3, has_conflicts=True),
            _item(
                pr_number=4,
                ci_status="failing",
                approved=False,
                failed_check_names=("verify / verify",),
                ticket_ids=("OMN-1",),
            ),
            _item(pr_number=5, ci_status="pending", approved=False),
        )
        output = await _run_over_bus(
            bus, ModelPrTriageInput(correlation_id=uuid4(), prs=prs)
        )
        assert output.total_green == 1
        assert output.total_red == 1
        assert output.total_conflicted == 1
        assert output.total_occ_dependency == 1
        assert output.total_needs_review == 1
        assert len(output.results) == 5
    finally:
        await bus.close()


@pytest.mark.integration
async def test_triage_empty_batch_over_bus(
    integration_event_bus: Any,
) -> None:
    """An empty inventory still round-trips to a terminal event with zero tallies."""
    bus = integration_event_bus
    await bus.start()
    try:
        correlation_id = uuid4()
        output = await _run_over_bus(
            bus, ModelPrTriageInput(correlation_id=correlation_id, prs=())
        )
        assert output.correlation_id == correlation_id
        assert output.results == ()
        assert (
            output.total_green
            + output.total_red
            + output.total_conflicted
            + output.total_occ_dependency
            + output.total_needs_review
            == 0
        )
    finally:
        await bus.close()


def test_triage_correlation_id_type_is_uuid() -> None:
    """The input model coerces the wire correlation_id into a real UUID so the
    handler preserves strong typing end-to-end."""
    model = ModelPrTriageInput(
        correlation_id="11111111-1111-1111-1111-111111111111",  # type: ignore[arg-type]
        prs=(),
    )
    assert isinstance(model.correlation_id, UUID)
