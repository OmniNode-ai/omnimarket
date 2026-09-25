# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every accepted task class reaches a terminal carrying its declared route (OMN-18831).

WHAT IS ASSERTED. The unified verification plan's row E11 states OMN-18831's
falsifier as: *a request of each accepted task class carries that class's
declared route in its terminal's route identity*. OMN-18831 exists because a
class decided the wrong thing about a request; the failure it produced was
reported with ``route UNATTRIBUTED`` and a provider-quota cause, so nobody
could read from the terminal which route the class had actually sent the
request down. This module closes the loop the other way: for every class the
gateway accepts, the route the contract declares for that class is the route
the terminal names.

"Declared route" is computed from the two contracts WITHOUT the routing
reducer's selection code: the first entry of the class's own
``escalation_policy.tier_order`` in ``task_class_contracts.v1.yaml``, and the
backends that tier in ``routing_tiers.yaml`` declares as serving the class in
``use_for``. The run is then driven through the real pieces: the routing
reducer's ``delta`` chooses, the orchestrator emits the inference intent, the
LLM-call effect's own provenance helper stamps the response from that intent,
and the orchestrator builds both terminals. Three identities must agree with
the declaration and with each other:

* the v2 completed terminal's ``backend_ref`` (the route-time identity,
  OMN-17802);
* the v1 completed terminal's ``route`` (the effect's report of what it
  called, OMN-18196);
* the terminal's ``task_type``, which must still be the class the request
  carried.

The negative control feeds one class's request the decision another class
would get, and shows the checker names the mismatch, so a green run is not a
checker that cannot fail.

Endpoints: the committed bifrost contract declares no endpoint for the local
backends (the lab supplies them by overlay), so nothing is routable in a
hermetic run. The fixture supplies an overlay giving the two local-tier
backends an unroutable ``.invalid`` endpoint. No call is made.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    load_task_class_authority,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    _provenance_stamp_fields,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
from omnimarket.routing.routing_tiers_path import ROUTING_TIERS_PACKAGED_DEFAULT_PATH

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)
_UNROUTABLE_ENDPOINT = "http://local-tier.invalid:8000/v1/chat/completions"


def _accepted_task_classes() -> list[str]:
    """The classes the gateway accepts: every ``gateway_exposure: public`` class."""
    authority = load_task_class_authority()
    return sorted(
        name
        for name, entry in authority.task_classes.items()
        if entry.gateway_exposure is EnumGatewayExposure.PUBLIC
    )


_ACCEPTED = _accepted_task_classes()


def _declared_route(task_class: str) -> tuple[str, frozenset[str]]:
    """The first declared tier, and the backends it declares for this class.

    Read from the two YAML contracts directly, never through the reducer, so
    the reducer is being checked against the declaration rather than against
    itself.
    """
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    tier_order = contract["task_classes"][task_class]["escalation_policy"]["tier_order"]
    first_tier = str(tier_order[0])
    tiers = yaml.safe_load(
        Path(ROUTING_TIERS_PACKAGED_DEFAULT_PATH).read_text(encoding="utf-8")
    )["tiers"]
    backends = frozenset(
        str(model["backend_id"])
        for tier in tiers
        if tier["name"] == first_tier
        for model in tier["models"]
        if task_class in (model.get("use_for") or ())
    )
    return first_tier, backends


def _local_tier_backend_ids() -> list[str]:
    tiers = yaml.safe_load(
        Path(ROUTING_TIERS_PACKAGED_DEFAULT_PATH).read_text(encoding="utf-8")
    )["tiers"]
    return sorted(
        {
            str(model["backend_id"])
            for tier in tiers
            if tier["name"] == "local"
            for model in tier["models"]
        }
    )


@pytest.fixture
def local_tier_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Give each local-tier backend an endpoint, as the lab overlay does."""
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": backend_id,
                        "endpoint_url": _UNROUTABLE_ENDPOINT,
                        "tier": "local",
                    }
                    for backend_id in _local_tier_backend_ids()
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._load_bifrost_endpoints.cache_clear()


def _request(task_class: str, correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Route-identity probe for the accepted task class " + task_class,
        task_type=task_class,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        # The house tenant: platform work, which the customer-key terminus
        # leaves alone, and a tenant the v2 terminal can carry.
        tenant_id=HOUSE_TENANT_SLUG,
    )


def _drive_to_completed_terminals(
    request: ModelDelegationRequest, decision: ModelRoutingDecision
) -> tuple[ModelDelegationCompleted, ModelDelegationTerminalCompletedV2]:
    """Run the real orchestrator legs, stamping the response as the effect does."""
    cid = request.correlation_id
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(request)
    emitted = list(handler.handle_routing_decision(decision))
    intents = [event for event in emitted if hasattr(event, "route")]
    assert len(intents) == 1, [type(event).__name__ for event in emitted]
    intent = intents[0]
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            inference_attempt_id=getattr(intent, "inference_attempt_id", None),
            content="route identity probe answer",
            model_used=decision.selected_model,
            llm_call_id="chatcmpl-omn18831-route",
            latency_ms=10,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            **_provenance_stamp_fields(intent, None),
        )
    )
    events = list(
        handler.handle_gate_result(
            ModelQualityGateResult(
                correlation_id=cid,
                passed=True,
                quality_score=1.0,
                failure_reasons=(),
                fallback_recommended=False,
            )
        )
    )
    v1 = [event for event in events if isinstance(event, ModelDelegationCompleted)]
    v2 = [
        event
        for event in events
        if isinstance(event, ModelDelegationTerminalCompletedV2)
    ]
    fanout = [type(event).__name__ for event in events]
    assert len(v1) == 1, fanout
    assert len(v2) == 1, fanout
    return v1[0], v2[0]


def _route_identity_violations(
    task_class: str,
    decision: ModelRoutingDecision,
    v1: ModelDelegationCompleted,
    v2: ModelDelegationTerminalCompletedV2,
) -> list[str]:
    """Every way the terminal's route identity can disagree with the declaration."""
    tier, declared = _declared_route(task_class)
    problems: list[str] = []
    if decision.tier_name != tier:
        problems.append(f"routed on tier {decision.tier_name!r}, declared {tier!r}")
    if v2.backend_ref not in declared:
        problems.append(
            f"v2 backend_ref {v2.backend_ref!r} is not a backend tier {tier!r} "
            f"declares for {task_class!r}: {sorted(declared)}"
        )
    if v1.route != v2.backend_ref:
        problems.append(
            f"v1 route {v1.route!r} disagrees with v2 backend_ref {v2.backend_ref!r}"
        )
    if v1.task_type != task_class or v2.task_type != task_class:
        problems.append(
            f"terminal task_type v1={v1.task_type!r} v2={v2.task_type!r}, "
            f"request carried {task_class!r}"
        )
    return problems


def test_the_accepted_classes_are_the_eleven_public_classes() -> None:
    """Pinned so the sweep below cannot shrink silently."""
    assert _ACCEPTED == [
        "code_generation",
        "code_review",
        "complex_reasoning",
        "document",
        "planning",
        "reasoning",
        "refactor",
        "research",
        "review",
        "summarization",
        "test",
    ]


@pytest.mark.parametrize("task_class", _ACCEPTED)
def test_every_accepted_class_declares_a_first_rung_backend(task_class: str) -> None:
    """A class with no declared backend on its first tier has no route to carry."""
    _tier, declared = _declared_route(task_class)
    assert declared, task_class


@pytest.mark.parametrize("task_class", _ACCEPTED)
@pytest.mark.usefixtures("local_tier_endpoints")
def test_the_terminal_carries_the_declared_route(task_class: str) -> None:
    request = _request(task_class, uuid4())
    decision = routing.delta(request, surface=EnumDelegationSurface.CLOUD)
    v1, v2 = _drive_to_completed_terminals(request, decision)

    assert _route_identity_violations(task_class, decision, v1, v2) == []


@pytest.mark.usefixtures("local_tier_endpoints")
def test_the_checker_names_a_route_the_class_did_not_declare() -> None:
    """Negative control: a code class answered on the prose backend is caught.

    ``code_generation``'s first tier declares ``local-coder``; ``document``'s
    declares ``local-heavy-reasoning``. Handing a code_generation request the
    decision a document request gets must be reported, or the sweep above
    proves nothing.
    """
    _tier, code_declared = _declared_route("code_generation")
    _tier, prose_declared = _declared_route("document")
    assert code_declared.isdisjoint(prose_declared), (
        "precondition: the control needs two classes with disjoint first rungs"
    )

    cid = uuid4()
    request = _request("code_generation", cid)
    wrong = routing.delta(
        _request("document", cid), surface=EnumDelegationSurface.CLOUD
    ).model_copy(update={"task_type": "code_generation"})
    v1, v2 = _drive_to_completed_terminals(request, wrong)

    violations = _route_identity_violations("code_generation", wrong, v1, v2)
    assert len(violations) == 1
    assert (
        "is not a backend tier 'local' declares for 'code_generation'"
        in (violations[0])
    )
