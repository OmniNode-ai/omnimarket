# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool mark event diff, its model and its two sinks (OMN-19998).

Each of the six mark changes yields exactly one event with the who/verb/host/label/cause the caller
passed and the cause the cause_for rule named; no change yields none. The model refuses an empty
``who``, a naive ``at``, an empty ``cause`` and a verb that changes no mark; the bus sink publishes
the event's JSON on the topic keyed by ``host:label`` and raises ``PoolMarkEventPublishError``
naming the topic alone, and the in-memory sink holds what was published. One control test injects
a diff that ignores ``revoked_at`` and shows the suite refusing it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_pool_mark_events import (
    BusPoolMarkEventSink,
    InMemoryPoolMarkEventSink,
    PoolMarkEventPublishError,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_pool_mark_diff import (
    mark_events,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
    ModelPoolMarkEvent,
    ModelPoolMarkEventsSettings,
)

pytestmark = pytest.mark.unit

WHO = "lab/h053/lane-9"
HOST = "h053"
LABEL = "onexbot"
TOPIC = "omnimarket.pool.marks"
ACTOR_ENV = "OMNI_LANE_ACTOR"
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
LIMITED_UNTIL = "2026-11-20T00:00:00Z"
NEWER_UNTIL = "2026-11-21T00:00:00Z"
REVOKED_AT = "2026-11-13T08:00:00Z"


def cause_for(verb: str, change: str) -> str:
    """The cause rule under test: it names the cause from the verb and the change."""
    return f"{verb}:{change}"


def an_event(
    *,
    verb: str = "mark_limited",
    change: str = "limited",
    cause: str = "lane asked",
) -> ModelPoolMarkEvent:
    return ModelPoolMarkEvent(
        who=WHO, at=NOW, verb=verb, host=HOST, label=LABEL, change=change, cause=cause
    )


def settings() -> ModelPoolMarkEventsSettings:
    return ModelPoolMarkEventsSettings(topic=TOPIC, actor_env=ACTOR_ENV)


GOLDEN = [
    pytest.param(
        {}, {"until": LIMITED_UNTIL}, "mark_limited", "limited", id="until-newly-set"
    ),
    pytest.param(
        {"until": LIMITED_UNTIL},
        {"until": NEWER_UNTIL},
        "apply_limit_rechecks",
        "limit_replaced",
        id="until-replaced-while-set",
    ),
    pytest.param(
        {"until": LIMITED_UNTIL},
        {},
        "apply_limit_rechecks",
        "limit_cleared",
        id="until-cleared",
    ),
    pytest.param(
        {},
        {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"},
        "mark_revoked",
        "revoked",
        id="revoked-newly-set",
    ),
    pytest.param(
        {},
        {"revoked_at": REVOKED_AT, "revoke_cause": "challenged"},
        "mark_revoked",
        "challenged",
        id="a-challenge-newly-set",
    ),
    pytest.param(
        {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"},
        {},
        "clear_revokes",
        "revoke_cleared",
        id="revoked-cleared",
    ),
]


@pytest.mark.parametrize(("before", "after", "verb", "change"), GOLDEN)
def test_golden_one_change_yields_exactly_one_event(
    before: dict[str, Any], after: dict[str, Any], verb: str, change: str
) -> None:
    events = mark_events(
        {LABEL: before},
        {LABEL: after},
        who=WHO,
        at=NOW,
        verb=verb,
        host=HOST,
        cause_for=cause_for,
    )
    assert len(events) == 1
    (event,) = events
    assert event.who == WHO
    assert event.verb == verb
    assert event.host == HOST
    assert event.label == LABEL
    assert event.change == change
    assert event.cause == cause_for(verb, change)
    assert event.at == NOW
    # the event carries no token and no fingerprint: exactly the seven fields
    assert set(json.loads(event.model_dump_json())) == {
        "who",
        "at",
        "verb",
        "host",
        "label",
        "change",
        "cause",
    }


def test_golden_no_change_yields_no_event() -> None:
    marks: dict[str, dict[str, Any]] = {
        LABEL: {"until": LIMITED_UNTIL},
        "second": {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"},
    }
    assert (
        mark_events(
            marks,
            marks,
            who=WHO,
            at=NOW,
            verb="sync_marks",
            host=HOST,
            cause_for=cause_for,
        )
        == ()
    )
    assert (
        mark_events(
            {}, {}, who=WHO, at=NOW, verb="sync_marks", host=HOST, cause_for=cause_for
        )
        == ()
    )
    # an until that is only in the past is not a change: the diff compares presence and value,
    # never the wall clock, because the caller hands it entries reduced to live marks
    past = {LABEL: {"until": "2000-01-01T00:00:00Z"}}
    assert (
        mark_events(
            past,
            past,
            who=WHO,
            at=NOW,
            verb="sync_marks",
            host=HOST,
            cause_for=cause_for,
        )
        == ()
    )
    # a key the diff does not read (uses, fingerprint) changing is no mark change
    uses = {LABEL: {"uses": 3, "until": LIMITED_UNTIL}}
    changed_uses = {LABEL: {"uses": 4, "until": LIMITED_UNTIL}}
    assert (
        mark_events(
            uses,
            changed_uses,
            who=WHO,
            at=NOW,
            verb="sync_marks",
            host=HOST,
            cause_for=cause_for,
        )
        == ()
    )


def test_golden_a_label_absent_from_before_reads_as_an_entry_without_marks() -> None:
    events = mark_events(
        {},
        {LABEL: {"until": LIMITED_UNTIL}},
        who=WHO,
        at=NOW,
        verb="add",
        host=HOST,
        cause_for=cause_for,
    )
    assert [(event.label, event.change) for event in events] == [(LABEL, "limited")]


def test_a_challenge_upgraded_to_a_real_revoke_while_the_stamp_stays_names_revoke_confirmed() -> (
    None
):
    before = {LABEL: {"revoked_at": REVOKED_AT, "revoke_cause": "challenged"}}
    after = {LABEL: {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"}}
    events = mark_events(
        before,
        after,
        who=WHO,
        at=NOW,
        verb="mark_revoked",
        host=HOST,
        cause_for=cause_for,
    )
    assert [(e.label, e.change) for e in events] == [(LABEL, "revoke_confirmed")]


def test_a_revoke_whose_cause_does_not_change_names_no_event() -> None:
    same = {LABEL: {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"}}
    assert (
        mark_events(
            same,
            same,
            who=WHO,
            at=NOW,
            verb="mark_revoked",
            host=HOST,
            cause_for=cause_for,
        )
        == ()
    )


def test_one_pass_that_replaces_a_limit_and_clears_a_revoke_names_both_limit_first() -> (
    None
):
    before = {
        LABEL: {
            "until": LIMITED_UNTIL,
            "revoked_at": REVOKED_AT,
            "revoke_cause": "challenged",
        }
    }
    after = {LABEL: {"until": NEWER_UNTIL}}
    events = mark_events(
        before,
        after,
        who=WHO,
        at=NOW,
        verb="apply_revoke_rechecks",
        host=HOST,
        cause_for=cause_for,
    )
    assert [(event.change, event.cause) for event in events] == [
        ("limit_replaced", cause_for("apply_revoke_rechecks", "limit_replaced")),
        ("revoke_cleared", cause_for("apply_revoke_rechecks", "revoke_cleared")),
    ]


def test_labels_are_named_in_sorted_order() -> None:
    before = {
        "zeta": {"until": LIMITED_UNTIL},
        "alpha": {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"},
    }
    after: dict[str, dict[str, Any]] = {"zeta": {}, "alpha": {}}
    events = mark_events(
        before,
        after,
        who=WHO,
        at=NOW,
        verb="sync_marks",
        host=HOST,
        cause_for=cause_for,
    )
    assert [(event.label, event.change) for event in events] == [
        ("alpha", "revoke_cleared"),
        ("zeta", "limit_cleared"),
    ]


def test_an_event_with_an_empty_who_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError) as raised:
        ModelPoolMarkEvent(
            who="",
            at=NOW,
            verb="mark_limited",
            host=HOST,
            label=LABEL,
            change="limited",
            cause="lane asked",
        )
    assert "who" in str(raised.value)


def test_the_diff_propagates_the_model_refusal_of_an_empty_who() -> None:
    with pytest.raises(ValidationError):
        mark_events(
            {},
            {LABEL: {"until": LIMITED_UNTIL}},
            who="",
            at=NOW,
            verb="mark_limited",
            host=HOST,
            cause_for=cause_for,
        )


def test_an_event_with_a_naive_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPoolMarkEvent(
            who=WHO,
            at=datetime(2026, 11, 14, 12, 0, 0),
            verb="mark_limited",
            host=HOST,
            label=LABEL,
            change="limited",
            cause="lane asked",
        )


def test_an_event_with_an_empty_cause_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPoolMarkEvent(
            who=WHO,
            at=NOW,
            verb="mark_limited",
            host=HOST,
            label=LABEL,
            change="limited",
            cause="",
        )


def test_an_event_with_a_verb_that_changes_no_mark_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPoolMarkEvent(
            who=WHO,
            at=NOW,
            verb="resolve",
            host=HOST,
            label=LABEL,
            change="limited",
            cause="lane asked",
        )


def test_an_event_at_with_an_offset_is_normalised_to_utc() -> None:
    event = ModelPoolMarkEvent(
        who=WHO,
        at=datetime(2026, 11, 14, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        verb="mark_limited",
        host=HOST,
        label=LABEL,
        change="limited",
        cause="lane asked",
    )
    assert event.at == NOW


def test_the_settings_carry_the_topic_and_the_actor_variable() -> None:
    assert (settings().topic, settings().actor_env) == (TOPIC, ACTOR_ENV)
    with pytest.raises(ValidationError):
        ModelPoolMarkEventsSettings(topic="", actor_env=ACTOR_ENV)
    with pytest.raises(ValidationError):
        ModelPoolMarkEventsSettings(topic=TOPIC, actor_env="")
    with pytest.raises(ValidationError):
        ModelPoolMarkEventsSettings(topic=TOPIC, actor_env=ACTOR_ENV, extra="no")


class RecordingBus:
    """A fake bus that holds what it was handed."""

    def __init__(self) -> None:
        self.publications: list[tuple[str, bytes, bytes]] = []

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        self.publications.append((topic, key, value))


class RefusingBus:
    """A fake bus whose publish refuses, echoing the payload it was handed into its message."""

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        raise RuntimeError(f"broker refused {value.decode('utf-8')}")


def test_the_bus_sink_publishes_the_event_json_on_the_topic_keyed_by_host_and_label() -> (
    None
):
    bus = RecordingBus()
    sink = BusPoolMarkEventSink(settings(), lambda: nullcontext(bus))
    event = an_event()
    sink.publish(event)
    assert bus.publications == [
        (TOPIC, b"h053:onexbot", event.model_dump_json().encode("utf-8"))
    ]


def test_the_bus_sink_raises_pool_mark_event_publish_error_naming_the_topic_alone() -> (
    None
):
    sink = BusPoolMarkEventSink(settings(), lambda: nullcontext(RefusingBus()))
    event = an_event()
    with pytest.raises(PoolMarkEventPublishError) as raised:
        sink.publish(event)
    error = raised.value
    assert error.topic == TOPIC
    assert error.error_type == "RuntimeError"
    assert str(error) == f"topic={TOPIC} error=RuntimeError"
    # the fake bus echoed the event's JSON into its own message; the sink's error carries
    # no value of the event, and the bus's refusal stays chained as the cause
    assert isinstance(error.__cause__, RuntimeError)
    for value in (WHO, HOST, LABEL, "lane asked", "mark_limited"):
        assert value not in str(error)


def test_the_in_memory_sink_holds_what_was_published() -> None:
    sink = InMemoryPoolMarkEventSink()
    first = an_event()
    second = an_event(
        verb="mark_revoked", change="challenged", cause="a 403 challenge page"
    )
    sink.publish(first)
    sink.publish(second)
    assert sink.events == [first, second]


def mark_events_ignoring_revoked_at(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    **kwargs: Any,
) -> tuple[ModelPoolMarkEvent, ...]:
    """The injected break: the same diff with every revoke change dropped, as a regression would do."""
    return tuple(
        event
        for event in mark_events(before, after, **kwargs)
        if event.change not in ("revoked", "challenged", "revoke_cleared")
    )


def test_failing_control_a_diff_that_ignores_revoked_at_is_caught() -> None:
    before = {LABEL: {"revoked_at": REVOKED_AT, "revoke_cause": "revoked"}}
    after: dict[str, dict[str, Any]] = {LABEL: {}}
    expected = (
        ModelPoolMarkEvent(
            who=WHO,
            at=NOW,
            verb="clear_revokes",
            host=HOST,
            label=LABEL,
            change="revoke_cleared",
            cause=cause_for("clear_revokes", "revoke_cleared"),
        ),
    )
    # the real diff names the event
    assert (
        mark_events(
            before,
            after,
            who=WHO,
            at=NOW,
            verb="clear_revokes",
            host=HOST,
            cause_for=cause_for,
        )
        == expected
    )
    # the break drops it, so the suite refuses a diff that ignores revoked_at
    with pytest.raises(AssertionError):
        assert (
            mark_events_ignoring_revoked_at(
                before,
                after,
                who=WHO,
                at=NOW,
                verb="clear_revokes",
                host=HOST,
                cause_for=cause_for,
            )
            == expected
        )


def test_the_contract_binds_the_diff_handler_and_it_answers_through_its_models() -> (
    None
):
    """The ratchet refuses a handler no contract binds: the contract names this class and its two models."""
    import importlib
    from pathlib import Path

    import yaml

    from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_pool_mark_diff import (
        HandlerPoolMarkDiff,
    )
    from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
        ModelPoolMarkDiffRequest,
    )

    node = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_claude_token_pool_effect"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text(encoding="utf-8"))
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "mark_diff"
    )
    assert entry["handler"]["name"] == HandlerPoolMarkDiff.__name__
    assert entry["handler"]["module"] == HandlerPoolMarkDiff.__module__
    model_module, _, model_name = entry["input_model"].rpartition(".")
    assert (
        getattr(importlib.import_module(model_module), model_name)
        is ModelPoolMarkDiffRequest
    )

    request = ModelPoolMarkDiffRequest(
        before={LABEL: {"until": None, "revoked_at": None, "revoke_cause": None}},
        after={
            LABEL: {"until": LIMITED_UNTIL, "revoked_at": None, "revoke_cause": None}
        },
        who=WHO,
        at=NOW,
        verb="mark_limited",
        host=HOST,
        cause="limit_estimate",
    )
    result = HandlerPoolMarkDiff().handle(request)
    assert [(e.label, e.change, e.cause) for e in result.events] == [
        (LABEL, "limited", "limit_estimate")
    ]
    with pytest.raises(ValidationError):
        ModelPoolMarkDiffRequest.model_validate({**request.model_dump(), "who": ""})
