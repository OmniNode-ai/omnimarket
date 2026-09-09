# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18092 -- the contract's declared ``event_model`` must be the model ``handle()`` takes.

RED-first. Every assertion names the defect class it pins.

THE DEFECT. This node was born (OMN-17201, omnimarket ``9d80e029``) declaring
three things that disagree: ``handler.input_model`` is
``ModelHookLedgerProjectionRequest``, all four ``handler_routing[].event_model``
entries are ``ModelHookLedgerInbound``, and ``handle()`` takes the former and
reads ``request.wire_topic`` on its first line. ``ModelHookLedgerInbound`` has
no such field.

WHY THAT DEAD-LETTERED EVERY RECORD RATHER THAN SOME. The shared runtime's
auto-wiring resolves the CONTRACT-DECLARED ``event_model``, validates the record
into it, and calls ``handle(typed_payload)``
(``omnibase_infra`` ``runtime/auto_wiring/handler_wiring.py``, the
``_import_event_model_class`` -> ``model_validate`` -> ``handle_method`` path).
``ModelHookLedgerInbound`` sets ``extra="allow"`` and requires only
``emitted_at``, so every hook record validates CLEANLY and the failure lands
inside ``handle()`` as an ``AttributeError`` instead of as a validation refusal.
A model that accepts everything cannot refuse anything, so the mismatch could
not surface at the seam built to catch it. Measured on the .201 dev lane:
100% of records on ``onex.evt.omniclaude.tool-executed.v1`` dead-lettered
between 2026-09-06T14:00Z and 2026-09-06T16:43Z.

WHY THIS TEST EXISTS EVEN THOUGH THE LANE IS QUIET. The drop stopped because
OMN-17985 (``f2115219``) pinned ``runtime_profiles:
[projection-writer-hook-ledger]``, which removes this cloud-only contract from
the shared kernel that was wiring it. That MASKS the mismatch; it does not fix
it. A future profile change, or any runtime that boots that profile name, walks
straight back into a 100% drop of four hook classes. The declaration is what has
to be correct, not the current set of processes that happen not to read it.

SCOPE. Node-scoped on purpose. A probe over all 163 ``handler_routing`` entries
in this repo that declare an ``event_model`` found 28 further typed mismatches,
almost all orchestrators whose ``handle()`` takes a union or workflow-input type
and coerces internally -- a pattern the wiring explicitly supports (OMN-13247).
Widening this into a repo-wide gate needs that pattern classified first;
asserting it here without that work would be a red test about somebody else's
design, which is how a gate gets disabled instead of obeyed.
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
    HandlerHookLedgerProjection,
)
from omnimarket.nodes.node_projection_hook_ledger.models import (
    model_hook_ledger_event,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_hook_ledger"
    / "contract.yaml"
)

#: A verbatim ``onex.evt.omniclaude.tool-executed.v1`` body, lifted from the
#: inner ``original_message`` of a real dead-lettered record on
#: ``onex.dlq.omnibase-infra.events.v1`` (offset 21782690, read 2026-09-09).
#: Used unmodified: a hand-built fixture is exactly how OMN-17919 kept a unit
#: suite green while 261 of 261 live records were rejected one leg up this same
#: chain.
LIVE_TOOL_EXECUTED_RECORD: dict[str, Any] = {
    "causation_id": None,
    "correlation_id": "9787a4a3-ec49-4819-8bdc-5044efb94550",
    "duration_ms": 865,
    "emitted_at": "2026-09-06T14:49:52.726700+00:00",
    "entity_id": "9787a4a3-ec49-4819-8bdc-5044efb94550",
    "hook_source": "post_tool_use",
    "interrupted": False,
    "schema_version": "1.0.0",
    "session_id": "9787a4a3-ec49-4819-8bdc-5044efb94550",
    "tool_name": "Bash",
    "working_directory": "omni_home",
}


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    with open(CONTRACT_PATH) as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict)
    return loaded


def _declared_event_models(contract: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(topic, event_model name)`` the contract's routing declares."""
    handlers = contract["handler_routing"]["handlers"]
    assert handlers, "contract declares no handler_routing handlers"
    return [(h["topic"], h["event_model"]["name"]) for h in handlers]


def _handle_input_model_name() -> str:
    """The model ``handle()`` actually accepts, read from its own signature.

    Resolved through ``typing.get_type_hints`` rather than by reading the raw
    annotation string, because the module carries
    ``from __future__ import annotations`` -- the raw form is text and would
    compare equal to a name that no longer resolves to anything.
    """
    hints = typing.get_type_hints(HandlerHookLedgerProjection.handle)
    params = [
        name
        for name, param in inspect.signature(
            HandlerHookLedgerProjection.handle
        ).parameters.items()
        if name != "self"
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    assert params, "handle() takes no payload parameter"
    annotation = hints[params[0]]
    return str(getattr(annotation, "__name__", annotation))


# ---------------------------------------------------------------------------
# AC1 -- the defect itself, reproduced on a real record
# ---------------------------------------------------------------------------


def test_declared_event_model_is_accepted_by_handle() -> None:
    """AC1: the model the runtime builds must be the model ``handle()`` takes.

    This is the whole defect in one assertion. Before the fix,
    ``ModelHookLedgerInbound.model_validate`` succeeds on a real record (it
    allows extras) and the resulting object reaches ``handle()``, which reads
    ``.wire_topic`` and raises ``AttributeError``.

    The assertion is on the FIELD, not on catching the exception. Asserting
    that ``handle()`` raises would keep passing after a fix that merely made
    it raise something tidier, and would go green if the contract were pointed
    at a third unrelated model. What has to be true is that the declared model
    carries the field the handler reads.
    """
    with open(CONTRACT_PATH) as fh:
        contract_doc = yaml.safe_load(fh)

    for topic, model_name in _declared_event_models(contract_doc):
        assert model_name == _handle_input_model_name(), (
            f"contract routes {topic} through event_model {model_name!r}, but "
            f"handle() takes {_handle_input_model_name()!r}. The runtime "
            "validates the record into the DECLARED model and passes it "
            "straight to handle(), so these two disagreeing is a 100% "
            "dead-letter of this topic, not a type-hint nit."
        )


def test_live_record_validated_into_declared_model_reaches_handle_intact() -> None:
    """AC1: a real dead-lettered record, put through the runtime's own coercion.

    Reproduces the runtime path with no handler construction and no database:
    validate the live record into the contract-declared model, then assert the
    result carries the attribute ``handle()`` reads first. Under the defect
    this fails on ``wire_topic``.
    """
    with open(CONTRACT_PATH) as fh:
        contract_doc = yaml.safe_load(fh)

    _, model_name = _declared_event_models(contract_doc)[0]
    # Resolved from the contract by NAME, the way the runtime resolves it --
    # not from a fixed mapping, which would keep testing a model the contract
    # no longer declares.
    model_cls = getattr(model_hook_ledger_event, model_name)

    if "record" in model_cls.model_fields:
        # The declared model WRAPS the producer body and carries the delivery
        # coordinates alongside it. Supplying them here is the point of the fix,
        # not a workaround for it: once the contract names this model, a bus
        # record WITHOUT them fails closed at validation (publisher_malformed,
        # one visible refusal per record) instead of crashing inside the
        # handler. The assertion is about the shape, not about the bus
        # providing it.
        payload: dict[str, Any] = {
            "wire_topic": (
                "tenant-beta-gateway-canary-79afa7263852."
                "onex.evt.omniclaude.tool-executed.v1"
            ),
            "record": dict(LIVE_TOOL_EXECUTED_RECORD),
            "partition": 0,
            "offset": 70192,
        }
    else:
        # A model that takes the producer body FLAT -- the shape the defect
        # declared. It validates the live record cleanly, which is exactly why
        # the mismatch never surfaced at the validation seam.
        payload = dict(LIVE_TOOL_EXECUTED_RECORD)

    typed = model_cls.model_validate(payload)

    assert hasattr(typed, "wire_topic"), (
        f"{model_name} carries no 'wire_topic', but handle() reads "
        "request.wire_topic on its first line. This is the exact "
        "AttributeError that dead-lettered every omniclaude hook record on "
        "the .201 dev lane between 2026-09-06T14:00Z and 2026-09-06T16:43Z."
    )


# ---------------------------------------------------------------------------
# AC2 -- the three declarations must name one model
# ---------------------------------------------------------------------------


def test_input_model_and_every_event_model_and_handle_agree(
    contract: dict[str, Any],
) -> None:
    """AC2: ``handler.input_model``, every ``event_model``, and ``handle()`` agree.

    Three places name the handler's payload type and nothing made them agree.
    The sibling ``node_projection_work_events`` names one model in both contract
    positions, which is the invariant this node broke on the day it was written.

    All four routing entries are checked rather than the first, because a
    partial correction that fixed ``tool-executed`` and left the other three
    would look fixed on the topic that happened to be measured while still
    dropping the other three hook classes entirely.
    """
    handle_model = _handle_input_model_name()

    declared_input = contract["handler"]["input_model"].rsplit(".", 1)[-1]
    assert declared_input == handle_model, (
        f"contract handler.input_model is {declared_input!r} but handle() "
        f"takes {handle_model!r}"
    )

    for topic, model_name in _declared_event_models(contract):
        assert model_name == handle_model, (
            f"handler_routing entry for {topic} declares event_model "
            f"{model_name!r}, which is neither handler.input_model nor the "
            f"model handle() takes ({handle_model!r})"
        )


def test_the_permissive_inbound_model_is_gone() -> None:
    """AC2: the model that could refuse nothing is not re-added.

    ``ModelHookLedgerInbound`` accepted every record (``extra="allow"``, one
    required field) and was referenced by nothing but the four contract slots
    that dead-lettered the lane. Re-adding it would re-arm the trap, because the
    obvious next step for anyone wiring this node into a shared runtime is to
    point ``event_model`` back at a model that validates a bare hook body.
    """
    assert not hasattr(model_hook_ledger_event, "ModelHookLedgerInbound"), (
        "ModelHookLedgerInbound is back. It is unreferenced by any code path "
        "in this node and exists only to be named by a contract slot, which is "
        "how the OMN-18092 dead-letter happened. The verbatim producer body is "
        "stored as JSONB and is deliberately not modelled."
    )


def test_every_hook_topic_is_routed(contract: dict[str, Any]) -> None:
    """AC2: the correction must not quietly drop a routing entry.

    The cheapest way to make the assertion above pass is to delete the routing
    entries that disagree. That would silence the test and leave the node
    undispatchable, so the four governed hook classes are pinned by name here.
    """
    routed = {topic for topic, _ in _declared_event_models(contract)}
    assert routed == set(contract["event_bus"]["subscribe_topics"]), (
        "handler_routing must route exactly the four hook classes the contract "
        f"subscribes to; routed={sorted(routed)}"
    )
