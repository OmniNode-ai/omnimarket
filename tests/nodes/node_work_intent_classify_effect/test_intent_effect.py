# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5, AC6, AC7, AC11: the lab-model fallback is local-only, undetermined-only, store-once (OMN-22263)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from omnimarket.models.work_classification.model_work_fallback import (
    EnumStoreStatus,
    ModelFallbackKey,
    ModelFallbackResult,
    ModelRouteAnswer,
)
from omnimarket.nodes.node_work_intent_classify_effect.handlers.handler_work_intent_classify import (
    HandlerWorkIntentClassify,
)
from omnimarket.nodes.node_work_intent_classify_effect.handlers.in_memory_ports import (
    InMemoryFallbackStore,
    ScriptedWorkIntentRoute,
)
from omnimarket.nodes.node_work_intent_classify_effect.models import (
    EnumIntentStatus,
    IntentRouteConfigError,
    ModelWorkIntentClassifyRequest,
    load_intent_route_config,
)

pytestmark = pytest.mark.unit

HOSTS = ("lab-a", "lab-b")
IDS = ("build", "fix", "plan")
GOOD_CONFIG = {
    "delegation_route": {
        "route_id": "scripted",
        "backend_class": "lab",
        "model_version": "m1",
    },
    "route_policy": "local_only",
    "lab_hosts": list(HOSTS),
    "timeout_s": 30,
}


def _request(**over: Any) -> ModelWorkIntentClassifyRequest:
    base: dict[str, Any] = {
        "unit_id": "u1",
        "current_work_type": "undetermined",
        "undetermined_id": "undetermined",
        "allowed_work_type_ids": IDS,
        "intent_text": "UNIQUETOKEN please fix the thing",
        "intent_digest": "sha256:d1",
        "rubric_version": "1.0.0",
        "prompt_template": "ids: {ids}\n{text}",
        "cause_no_route": "no-route",
        "cause_no_intent_text": "no-intent-text",
        "cause_route_invalid": "route-invalid",
        "config": load_intent_route_config(GOOD_CONFIG),
    }
    base.update(over)
    return ModelWorkIntentClassifyRequest(**base)


def _answer(
    work_type: str, receipt: str = "r1", host: str = "lab-a"
) -> ModelRouteAnswer:
    return ModelRouteAnswer(
        work_type_id=work_type,
        route_receipt_id=receipt,
        host=host,
        route_model_version="m1",
    )


def test_a_unit_decided_by_a_rule_produces_no_delegation_request() -> None:
    route = ScriptedWorkIntentRoute([_answer("fix")])
    handler = HandlerWorkIntentClassify(route=route, store=InMemoryFallbackStore())
    result = handler.handle(_request(current_work_type="build"))
    assert result.status is EnumIntentStatus.NOT_REACHED
    assert route.requests == []


def test_an_undetermined_unit_is_sent_to_the_route_and_keeps_the_returned_id() -> None:
    route = ScriptedWorkIntentRoute([_answer("fix")])
    handler = HandlerWorkIntentClassify(route=route, store=InMemoryFallbackStore())
    result = handler.handle(_request())
    assert result.status is EnumIntentStatus.DECIDED
    assert result.work_type_id == "fix"
    assert result.route_receipt_id == "r1"
    assert len(route.requests) == 1


def test_with_no_route_in_the_overlay_the_unit_is_undetermined_with_cause_no_route() -> (
    None
):
    route = ScriptedWorkIntentRoute([_answer("fix")])
    handler = HandlerWorkIntentClassify(route=route, store=InMemoryFallbackStore())
    result = handler.handle(_request(config=None))
    assert result.status is EnumIntentStatus.UNDETERMINED
    assert result.work_type_id == "undetermined"
    assert result.cause == "no-route"
    assert route.requests == []


def test_a_handler_built_with_no_ports_leaves_the_unit_undetermined_with_cause_no_route() -> (
    None
):
    """The infra auto-wiring builds the handler with no ports: nothing is fabricated."""
    result = HandlerWorkIntentClassify().handle(_request())
    assert result.status is EnumIntentStatus.UNDETERMINED
    assert result.work_type_id == "undetermined"
    assert result.cause == "no-route"
    route = ScriptedWorkIntentRoute([_answer("fix")])
    only_route = HandlerWorkIntentClassify(route=route).handle(_request())
    assert only_route.cause == "no-route"
    assert route.requests == []


def test_a_unit_with_no_intent_text_is_not_sent_to_the_route() -> None:
    route = ScriptedWorkIntentRoute([_answer("fix")])
    handler = HandlerWorkIntentClassify(route=route, store=InMemoryFallbackStore())
    result = handler.handle(_request(intent_text=None))
    assert result.cause == "no-intent-text"
    assert route.requests == []


def test_an_overlay_naming_a_cloud_backend_is_refused_at_load_naming_the_key() -> None:
    cloud = {
        **GOOD_CONFIG,
        "delegation_route": {
            "route_id": "x",
            "backend_class": "cloud",
            "model_version": "m1",
        },
    }
    with pytest.raises(
        IntentRouteConfigError, match=r"delegation_route\.backend_class"
    ):
        load_intent_route_config(cloud)


def test_a_policy_other_than_local_only_is_refused_at_load_naming_the_key() -> None:
    with pytest.raises(IntentRouteConfigError, match="route_policy"):
        load_intent_route_config({**GOOD_CONFIG, "route_policy": "any"})


def test_the_request_log_holds_only_declared_lab_hosts_and_the_local_only_policy() -> (
    None
):
    route = ScriptedWorkIntentRoute([_answer("fix")])
    HandlerWorkIntentClassify(route=route, store=InMemoryFallbackStore()).handle(
        _request()
    )
    assert route.requests
    for logged in route.requests:
        assert set(logged.hosts) <= set(HOSTS)
        assert logged.policy == "local_only"


def test_an_answer_from_a_host_outside_the_declared_lab_hosts_is_not_accepted_or_stored() -> (
    None
):
    store = InMemoryFallbackStore()
    route = ScriptedWorkIntentRoute([_answer("fix", host="elsewhere")])
    result = HandlerWorkIntentClassify(route=route, store=store).handle(_request())
    assert result.status is EnumIntentStatus.UNDETERMINED
    assert result.cause == "route-invalid"
    assert (
        store.get(
            ModelFallbackKey(
                intent_digest="sha256:d1",
                rubric_version="1.0.0",
                route_model_version="m1",
            )
        )
        is None
    )


def test_an_id_the_rubric_does_not_declare_is_not_accepted_or_stored() -> None:
    route = ScriptedWorkIntentRoute([_answer("unheard-of")])
    result = HandlerWorkIntentClassify(
        route=route, store=InMemoryFallbackStore()
    ).handle(_request())
    assert result.cause == "route-invalid"
    assert result.work_type_id == "undetermined"


def test_no_prompt_text_is_in_the_result_the_request_dump_or_the_route_log() -> None:
    route = ScriptedWorkIntentRoute([_answer("fix")])
    request = _request()
    result = HandlerWorkIntentClassify(
        route=route, store=InMemoryFallbackStore()
    ).handle(request)
    assert "UNIQUETOKEN" not in result.model_dump_json()
    assert "UNIQUETOKEN" not in repr(request)
    assert all("UNIQUETOKEN" not in repr(entry) for entry in route.requests)


def test_the_request_round_trips_through_its_dump_and_its_json_with_the_text() -> None:
    request = _request()
    assert (
        ModelWorkIntentClassifyRequest.model_validate(request.model_dump()) == request
    )
    assert (
        ModelWorkIntentClassifyRequest.model_validate_json(request.model_dump_json())
        == request
    )


def test_a_second_call_for_the_same_key_reuses_the_stored_result_without_the_route() -> (
    None
):
    store = InMemoryFallbackStore()
    route = ScriptedWorkIntentRoute([_answer("fix", "r1"), _answer("plan", "r2")])
    handler = HandlerWorkIntentClassify(route=route, store=store)
    first = handler.handle(_request())
    second = handler.handle(_request(unit_id="u2"))
    assert (first.work_type_id, second.work_type_id) == ("fix", "fix")
    assert second.status is EnumIntentStatus.CACHED
    assert len(route.requests) == 1


def test_a_different_route_model_version_is_a_different_key() -> None:
    store = InMemoryFallbackStore()
    route = ScriptedWorkIntentRoute(
        [
            _answer("fix", "r1"),
            ModelRouteAnswer(
                work_type_id="plan",
                route_receipt_id="r2",
                host="lab-a",
                route_model_version="m2",
            ),
        ]
    )
    handler = HandlerWorkIntentClassify(route=route, store=store)
    handler.handle(_request())
    newer = {
        **GOOD_CONFIG,
        "delegation_route": {
            "route_id": "scripted",
            "backend_class": "lab",
            "model_version": "m2",
        },
    }
    again = handler.handle(
        _request(unit_id="u2", config=load_intent_route_config(newer))
    )
    assert again.status is EnumIntentStatus.DECIDED
    assert again.work_type_id == "plan"


def test_an_answer_from_a_different_route_model_version_than_the_overlay_names_is_invalid() -> (
    None
):
    route = ScriptedWorkIntentRoute(
        [
            ModelRouteAnswer(
                work_type_id="fix",
                route_receipt_id="r1",
                host="lab-a",
                route_model_version="other",
            )
        ]
    )
    result = HandlerWorkIntentClassify(
        route=route, store=InMemoryFallbackStore()
    ).handle(_request())
    assert result.cause == "route-invalid"


def test_concurrent_calls_for_one_key_store_one_result_and_refuse_and_count_the_other() -> (
    None
):
    store = InMemoryFallbackStore()
    barrier = threading.Barrier(2)
    route = ScriptedWorkIntentRoute(
        [_answer("fix", "r1"), _answer("plan", "r2")], barrier=barrier
    )
    results = []

    def call(unit: str) -> None:
        results.append(
            HandlerWorkIntentClassify(route=route, store=store).handle(
                _request(unit_id=unit)
            )
        )

    threads = [threading.Thread(target=call, args=(name,)) for name in ("ua", "ub")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    key = ModelFallbackKey(
        intent_digest="sha256:d1", rubric_version="1.0.0", route_model_version="m1"
    )
    stored = store.get(key)
    assert stored is not None
    assert {r.work_type_id for r in results} == {stored.work_type_id}
    assert store.refused_count(key) == 1
    assert sum(1 for r in results if r.refused) == 1


def test_put_if_absent_answers_stored_same_then_refused_and_never_overwrites() -> None:
    store = InMemoryFallbackStore()
    key = ModelFallbackKey(
        intent_digest="d", rubric_version="1", route_model_version="m"
    )
    a = ModelFallbackResult(work_type_id="fix", route_receipt_id="r1")
    b = ModelFallbackResult(work_type_id="plan", route_receipt_id="r2")
    assert store.put_if_absent(key, a).status is EnumStoreStatus.STORED
    assert store.put_if_absent(key, a).status is EnumStoreStatus.SAME
    refused = store.put_if_absent(key, b)
    assert refused.status is EnumStoreStatus.REFUSED
    assert refused.stored == a
    assert store.get(key) == a
    assert store.refused_count(key) == 1
