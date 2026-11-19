# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from socketserver import StreamRequestHandler
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder import (
    CoreRuntimeBusForwarder,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.handlers import (
    HandlerRuntimeBusForward,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models import (
    ModelForwardEvent,
    ModelRuntimeBusForwardCommand,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    NODE_NAME,
    ModelRuntimeBusForwardOverlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    PublisherRefusedError,
)

from .https_test_door import (
    AUTH_REF,
    ROUTE,
    TOKEN,
    TOPIC,
    URL,
    URL_REF,
    RecordingDoor,
    accepted,
    document,
    loopback_door,
    outcome,
    overlay,
    records,
    respond,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_https import (
        PublisherHttps,
    )

pytestmark = pytest.mark.unit


@dataclass
class Clock:
    seconds: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds


def publisher(
    configuration: ModelRuntimeBusForwardOverlay,
    *,
    url: str = URL,
    token: str = TOKEN,
    door: RecordingDoor | None = None,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    clock: Clock | None = None,
) -> PublisherHttps:
    from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_https import (
        PublisherHttps,
    )

    config = configuration.event_bus
    assert config.type == "https"
    return PublisherHttps(
        config,
        url=url,
        token=token,
        transport_factory=door.transport if door else transport_factory,
        clock=clock.monotonic if clock else time.monotonic,
        sleep=clock.sleep if clock else time.sleep,
    )


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def event(
    value: bytes,
    *,
    key: bytes | None = None,
    headers: dict[str, str] | None = None,
    topic: str = TOPIC,
) -> ModelForwardEvent:
    return ModelForwardEvent(
        topic=topic,
        key_b64=None if key is None else b64(key),
        value_b64=b64(value),
        headers={} if headers is None else headers,
    )


def receipt(
    configuration: ModelRuntimeBusForwardOverlay,
    door: RecordingDoor,
    *events: ModelForwardEvent,
    clock: Clock | None = None,
) -> dict[str, object]:
    sender = publisher(configuration, door=door, clock=clock or Clock())
    return (
        HandlerRuntimeBusForward(overlay=configuration, publisher=sender)
        .handle(ModelRuntimeBusForwardCommand(run_id="synthetic-run", events=events))
        .receipt()
    )


def values(door: RecordingDoor) -> list[list[bytes]]:
    return [
        [base64.b64decode(row["value"]) for row in records(r.content)]
        for r in door.requests
    ]


# - the https publisher posts every recorded event in order with its key, value and headers to the
# declared URL in batches no larger than the overlay bound, and an in-memory run's receipt counts
# forwarded and failed -- falsifier: `pytest ... -k https_golden` with the batch splitter removed
# turns the 3-batch case red
def test_https_golden_forwarder_loads_overlay_and_batches_an_in_memory_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_kafka(*args: object) -> None:
        pytest.fail("HTTPS entered the Kafka broker factory")

    import omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder as adapter

    door = RecordingDoor()
    monkeypatch.setattr(adapter, "broker_for_lane", unexpected_kafka)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: door.transport())
    path = tmp_path / NODE_NAME / "overlay.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document(max_batch_records=2)), encoding="utf-8")
    configuration = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        URL_REF: URL,
        AUTH_REF: TOKEN,
    }
    originals = tuple(
        event(bytes([index, 0xFF]), key=bytes([index]), headers={"trace": f"r-{index}"})
        for index in range(5)
    )

    result = CoreRuntimeBusForwarder(environ=configuration).forward(
        (*originals, event(b"outside", topic="outside.topic"))
    )

    assert (result["forwarded"], result["failed"]) == (5, 1)
    assert [len(records(request.content)) for request in door.requests] == [2, 2, 1]
    observed = []
    for request in door.requests:
        assert request.method == "POST"
        assert str(request.url) == URL
        observed.extend(records(request.content))
    assert [
        (
            row["topic"],
            base64.b64decode(row["key"]),
            base64.b64decode(row["value"]),
            row["headers"],
        )
        for row in observed
    ] == [
        (item.topic, item.key_bytes(), item.value_bytes(), item.headers)
        for item in originals
    ]


async def test_https_golden_runtime_settle_reads_the_registered_forwarder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_core.models.event_bus.model_bus_settle import ModelBusSettle
    from omnibase_core.models.event_bus.model_runtime_bus_event import (
        ModelRuntimeBusEvent,
    )

    from omnimarket.inference import secret_store_resolver

    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    secret_store_resolver.clear_secret_store_resolver_cache()
    try:
        path = tmp_path / NODE_NAME / "overlay.yaml"
        path.parent.mkdir()
        path.write_text(
            yaml.safe_dump(
                document(
                    max_batch_records=2,
                    max_event_bytes=1024,
                    max_record_headers=24,
                    max_header_name_chars=64,
                    max_header_value_chars=128,
                )
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
        monkeypatch.setenv("GATEWAY_INGEST_URL", URL)
        monkeypatch.setenv("GATEWAY_INGEST_TOKEN", TOKEN)
        door = RecordingDoor()
        monkeypatch.setattr(
            httpx, "AsyncHTTPTransport", lambda **_kwargs: door.transport()
        )

        bus = EventBusInmemory()
        await bus.start()
        for index in range(5):
            await bus.publish(TOPIC, bytes([index]), bytes([0xFF, index]))
        await bus.publish("outside.topic", None, b"refused")
        originals = [
            ModelRuntimeBusEvent.from_message(m) for m in await bus.get_event_history()
        ]
        result = await ModelBusSettle.run(bus, {"event_bus": "inmemory"})

        assert result["events_recorded"] == 6
        assert (result["events_forwarded"], result["events_failed"]) == (5, 1)
        assert [len(records(request.content)) for request in door.requests] == [2, 2, 1]
        assert [
            (row["topic"], row["key"], row["value"], row["headers"])
            for request in door.requests
            for row in records(request.content)
        ] == [(e.topic, e.key_b64, e.value_b64, e.headers) for e in originals[:-1]]
    finally:
        secret_store_resolver.clear_secret_store_resolver_cache()


# - a `422` refuses its whole sub-batch: every record in it counts failed, the bad ones named in
# `failure_detail` by the door's reasons, and it is not retried (one request only) -- falsifier:
# `pytest ... -k https_refusal` with the `422` branch made retryable turns the request-count
# assertion red
def test_https_refusal_fails_the_whole_sub_batch_without_retry() -> None:
    door = RecordingDoor(
        lambda *_: httpx.Response(
            422,
            json={
                "refusals": [
                    {"index": 0, "reason": "topic-not-allowed"},
                    {"index": 2, "reason": "header-not-allowed"},
                ]
            },
        )
    )
    result = receipt(
        overlay(), door, event(b"bad"), event(b"valid"), event(b"also bad")
    )
    assert (result["forwarded"], result["failed"], len(door.requests)) == (0, 3, 1)
    detail = str(result["detail"])
    assert "topic-not-allowed" in detail
    assert "header-not-allowed" in detail


# - a `503` with a partial body counts the body's `published` records (each confirmed by its own
# delivery report) forwarded, which are the acknowledged ones, and the rest failed (a `failed`
# record with reason `delivery-unconfirmed` may already be on the broker, and the client says so in
# `failure_detail`); the next attempt resends only the records not yet acknowledged, and a final
# failure leaves `forwarded + failed` equal to the events handed in -- falsifier: `pytest ... -k
# https_partial` with the body ignored (whole sub-batch counted failed) turns the forwarded-count
# case red
def test_https_partial_retries_only_the_unacknowledged_suffix() -> None:
    door = RecordingDoor(lambda number, req: outcome(req, 1 if number == 1 else 0))
    original = (event(b"confirmed"), event(b"uncertain"), event(b"later"))
    result = receipt(overlay(), door, *original)

    assert (result["forwarded"], result["failed"]) == (1, 2)
    assert result["forwarded"] + result["failed"] == len(original)
    assert values(door) == [
        [b"confirmed", b"uncertain", b"later"],
        [b"uncertain", b"later"],
    ]
    assert "delivery-unconfirmed" in str(result["detail"])
    assert "may have been published" in str(result["detail"])


# - serialized size bounds (review round 6, `-k https_size_bounds`): a record whose serialized form
# (base64 included) is one byte over `max_event_bytes` is refused at `publish`, never sent, counted
# not confirmed and named `event-too-large`, and one at exactly the bound is sent; a run of records
# whose serialized batch would pass `max_batch_bytes` before it passes `max_batch_records` is split
# so that the largest body the `MockTransport` door received is at most `max_batch_bytes` (asserted
# from the received body length, not from the count); a resolved `authorization` value over
# `max_authorization_bytes` sends nothing; the load refuses `max_batch_bytes` below
# `max_event_bytes` plus the envelope -- falsifier: `pytest ... -k https_size_bounds` with the
# splitter counting decoded rather than serialized bytes turns the body-length case red, and with
# the event check removed turns the refusal case red
def test_https_size_bounds_measure_the_actual_serialized_event_and_request() -> None:
    door = RecordingDoor()
    configuration = overlay()
    valid = b"\xff" * 63
    sender = publisher(configuration, door=door)
    sender.publish(TOPIC, None, valid, {})
    assert sender.flush(60) == 0
    encoded = door.requests[0].content.removeprefix(b'{"records":[').removesuffix(b"]}")
    size = len(encoded)
    limit = size + 1
    envelope = len(b'{"records":[]}')
    with pytest.raises(ValidationError, match="max_batch_bytes"):
        overlay(max_event_bytes=limit, max_batch_bytes=envelope + limit - 1)
    overlay(max_event_bytes=limit, max_batch_bytes=envelope + limit)
    # One more ASCII character in a header value increases the wire size by one byte.
    with_header = RecordingDoor()
    reference = publisher(configuration, door=with_header)
    reference.publish(TOPIC, None, valid, {"x": "a"})
    assert reference.flush(60) == 0
    with_header_size = len(with_header.requests[0].content) - envelope
    assert with_header_size > limit
    bound = overlay(
        max_batch_records=10,
        request_timeout_seconds=12,
        max_event_bytes=with_header_size,
        max_batch_bytes=envelope + 2 * with_header_size,
    )
    sender = publisher(bound, door=door, clock=Clock())
    with pytest.raises(PublisherRefusedError, match="event-too-large"):
        sender.publish(TOPIC, None, valid, {"x": "ab"})
    assert "event-too-large" in str(sender.failure_detail())
    for _ in range(5):
        sender.publish(TOPIC, None, valid, {"x": "a"})
    assert sender.flush(600) == 0
    batch_requests = door.requests[1:]
    assert all(
        len(r.content) <= bound.event_bus.max_batch_bytes for r in batch_requests
    )
    assert [len(records(r.content)) for r in batch_requests] == [1, 1, 1, 1, 1]
    assert all(
        row["headers"] == {"x": "a"}
        for r in batch_requests
        for row in records(r.content)
    )


def test_https_size_bounds_overlong_resolved_authorization_sends_nothing() -> None:
    door = RecordingDoor()
    configuration = overlay(max_authorization_bytes=len("Bearer ") + 4)
    sender = publisher(configuration, token="12345", door=door, clock=Clock())
    sender.publish(TOPIC, None, b"value", {})
    assert sender.flush(60) == 1
    assert not door.requests
    assert "max_authorization_bytes" in str(sender.failure_detail())

    sender = publisher(configuration, token="1234", door=door, clock=Clock())
    sender.publish(TOPIC, None, b"value", {})
    assert sender.flush(60) == 0
    assert len(door.requests) == 1


# - field limits (`-k https_field_bounds`, review round 9 new 4): for each of the six field limits
# in turn, a record whose one field is a byte (or a character, for header names, header values and
# the topic; one header, for the count) over its limit while its serialized size is well under
# `max_event_bytes` is refused at `publish`, never sent, counted not confirmed and named
# `event-field-too-large` with the field name in `failure_detail`, and a record with that field
# exactly at its limit is sent; a key of `max_key_bytes + 1` bytes inside a record that passes the
# aggregate check is the case that fails without this rule -- falsifier: `pytest ... -k
# https_field_bounds` with only the aggregate `max_event_bytes` check kept turns every over-limit
# field case red (the record is sent), and with the key limit compared on its base64 length instead
# of its decoded bytes turns the exact-limit key case red
@pytest.mark.parametrize(
    ("limit", "field_name", "exact", "over"),
    [
        ("max_key_bytes", "key", {"key": b"abcd"}, {"key": b"abcde"}),
        ("max_value_bytes", "value", {"value": b"abcd"}, {"value": b"abcde"}),
        (
            "max_record_headers",
            "headers",
            {"headers": {"x": "a", "y": "b", "z": "c", "w": "d"}},
            {"headers": {"x": "a", "y": "b", "z": "c", "w": "d", "q": "e"}},
        ),
        (
            "max_header_name_chars",
            "header name",
            {"headers": {"abcd": "v"}},
            {"headers": {"abcde": "v"}},
        ),
        (
            "max_header_value_chars",
            "header value",
            {"headers": {"x": "éééé"}},
            {"headers": {"x": "ééééé"}},
        ),
        ("max_topic_chars", "topic", {"topic": "abcd"}, {"topic": "abcde"}),
    ],
)
def test_https_field_bounds_count_each_field_before_encoding(
    limit: str, field_name: str, exact: dict, over: dict
) -> None:
    door = RecordingDoor()
    settings = overlay(**{limit: 4})
    sender = publisher(settings, door=door, clock=Clock())
    defaults = {"topic": TOPIC, "key": None, "value": b"val", "headers": {}}
    with pytest.raises(PublisherRefusedError, match="event-field-too-large") as refused:
        sender.publish(**(defaults | over))
    assert limit in str(refused.value)
    assert field_name in str(refused.value)
    assert limit in str(sender.failure_detail())
    sender.publish(**(defaults | exact))
    assert sender.flush(60) == 0
    assert len(door.requests) == 1
    assert len(records(door.requests[0].content)) == 1


# - dedup identity at the client (`-k https_identity`, review round 8 finding 1): two records
# handed in with the same topic and value and different keys, and two with the same topic, key and
# value and different declared identity headers, reach the door stand-in with four different
# `event_id`s and four records, and the receipt reads forwarded 4, failed 0; a rerun of the same
# four reaches the stand-in with the same four ids and the receipt reads forwarded 4 whenever the
# stand-in answers each `published`, because the client counts acknowledgments and relies on no
# first write (at-least-once, section 3 Delivery guarantee) -- falsifier: `pytest ... -k
# https_identity` with the publisher computing the id from topic and value only turns the key and
# header cases red (the stand-in sees two distinct ids instead of four)
def test_https_identity_uses_keys_and_shared_identity_headers_on_each_run() -> None:
    from omnibase_core.models.contracts.model_ingest_contract import (
        IDENTITY_HEADERS,
        compute_event_id,
    )

    door = RecordingDoor()
    identity = IDENTITY_HEADERS[0]
    inputs = (
        event(b"same", key=b"k1"),
        event(b"same", key=b"k2"),
        event(b"same", key=b"k3", headers={identity: "a"}),
        event(b"same", key=b"k3", headers={identity: "b"}),
    )
    for _ in range(2):
        assert receipt(overlay(), door, *inputs) == {
            "forwarded": 4,
            "failed": 0,
            "detail": None,
        }
    first, second = (
        [row["event_id"] for row in records(r.content)] for r in door.requests
    )
    assert second == first
    assert len(set(first)) == 4
    assert first == [
        compute_event_id(
            item.topic,
            item.key_bytes(),
            item.value_bytes(),
            {name: value.encode("utf-8") for name, value in item.headers.items()},
        )
        for item in inputs
    ]


# - header budget (`-k https_header_budget`, review round 8 finding 4): the real `httpx.Client` the
# publisher builds (no `MockTransport`) sends one flush with a maximum-length `authorization` over
# a real loopback socket that records the raw request head; the header names on the wire equal
# `CLIENT_HEADER_SET` exactly (no extra name, no missing one), each value is within its declared
# length, and the head's header count and bytes are within the shared declared worst case (the
# door's limits sit above it by `HEADER_BUDGET_MARGIN`); building a request with an added header
# name raises before anything is sent -- falsifier: `pytest ... -k https_header_budget` with a
# library default header (`user-agent` with a long value) left unsuppressed turns the exact-set
# case red, and with the send-time subset check removed turns the added-header case red
def test_https_header_budget_on_the_wire_and_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_core.models.contracts.model_ingest_contract import CLIENT_HEADER_SET

    configuration = overlay(max_authorization_bytes=64)
    with loopback_door(tmp_path) as (url, verify, observed):
        sender = publisher(
            configuration,
            url=url,
            token="t" * (64 - len("Bearer ")),
            transport_factory=lambda: httpx.AsyncHTTPTransport(verify=verify),
        )
        sender.publish(TOPIC, None, b"payload", {})
        assert (
            sender.flush(5) == 1
        )  # No clipped first attempt when the full timeout is six.
        assert not observed
        sender.publish(TOPIC, None, b"payload", {})
        assert sender.flush(30) == 0
        raw_head, body = observed[0]
        request_line, *headers = raw_head.removesuffix(b"\r\n\r\n").split(b"\r\n")
        assert request_line == f"POST {ROUTE} HTTP/1.1".encode("ascii")
        names_and_values = [
            (name.lower().decode("ascii"), value.removeprefix(b" "))
            for name, value in (line.split(b":", 1) for line in headers)
        ]
        limits = {
            name: 64 if limit == "max_authorization_bytes" else limit
            for name, limit in CLIENT_HEADER_SET.items()
        }
        assert len(names_and_values) == len(limits)
        assert {name for name, _ in names_and_values} == set(limits)
        assert all(len(value) <= limits[name] for name, value in names_and_values)
        assert dict(names_and_values)["content-length"] == str(len(body)).encode(
            "ascii"
        )
        assert len(dict(names_and_values)["authorization"]) == 64
        assert (
            len(raw_head) - len(request_line) - 2
            <= sum(len(name) + 4 + bound for name, bound in limits.items()) + 2
        )

        original = httpx.Request

        def with_extra(*args: object, **kwargs: object) -> httpx.Request:
            request = original(*args, **kwargs)
            request.headers["user-agent"] = "undeclared" * 100
            return request

        monkeypatch.setattr(httpx, "Request", with_extra)
        sender.publish(TOPIC, None, b"keep off the wire", {})
        with pytest.raises(PublisherRefusedError, match="user-agent"):
            sender.flush(30)
        assert len(observed) == 1
        assert sender.flush(30) == 0


# - deadline load check (`-k https_deadline_load`, review round 8 finding 5): an overlay with
# `request_timeout_seconds` equal to `max_batch_records x door_record_confirm_timeout_seconds` is
# refused at load, one second above that plus `request_margin_seconds` loads, an absent
# `door_record_confirm_timeout_seconds` or `request_margin_seconds` refuses the load -- falsifier:
# `pytest ... -k https_deadline_load` with the margin dropped from the inequality turns the
# boundary case red
# - deadline load boundary (`-k https_deadline_load`, completing the deadline load check above): an
# overlay with `request_timeout_seconds` exactly equal to `max_batch_records x
# door_record_confirm_timeout_seconds + request_margin_seconds` is refused at load, because the
# inequality of section 3 Deadline join is strict -- falsifier: `pytest ... -k https_deadline_load`
# with the margin dropped from the inequality turns this case red (the overlay loads); the
# bare-product case above stays refused under that mutation, so this case, not that one, is what
# holds the margin
def test_https_deadline_load_uses_a_strict_join_and_explicit_inputs() -> None:
    assert overlay(request_timeout_seconds=6).event_bus.type == "https"
    for refused in (4, 5):
        with pytest.raises(ValidationError, match="request_timeout_seconds"):
            overlay(request_timeout_seconds=refused)
    for key in ("door_record_confirm_timeout_seconds", "request_margin_seconds"):
        data = document()
        del data["event_bus"][key]
        with pytest.raises(ValidationError, match=key):
            ModelRuntimeBusForwardOverlay.model_validate(data)


# - flush budget (`-k https_flush_budget`, review round 11 item 2 and round 12 item 1): the load
# refuses `max_attempts` of 1, and refuses `flush_timeout_seconds` one second below
# `worst_case_sub_batches x (2 x request_timeout_seconds + retry_backoff_seconds)` for
# `worst_case_sub_batches = ceil(max_events / records_per_request)` taken at the byte split of
# section 3 Deadline join (the old rule's value, `request_timeout_seconds` alone, is one of the
# refused cases, and so is the single-sub-batch value when `max_events` needs 5 sub-batches), and
# loads at exactly that product. **Byte split (round 12):** an overlay with `max_events` 500,
# `max_batch_records` 100 and `max_event_bytes` and `max_batch_bytes` chosen so that only 10
# maximum-size events fit one request has `worst_case_sub_batches` 50: the load refuses the
# 5-sub-batch product (what the records-only rule computes) and loads at the 50-sub-batch product.
# **Reservation (round 12), run time on the virtual clock with `max_attempts` 4 and 5 actual
# sub-batches:** every attempt on sub-batch 1 fails at the full timeout and the door would answer
# `202` only on a fourth attempt; the client sends sub-batch 1's second attempt, and sends its
# third only while the remaining budget still covers `retry_backoff_seconds +
# request_timeout_seconds + later x (2 x request_timeout_seconds + retry_backoff_seconds)`, so it
# stops retrying sub-batch 1 at the point where the next attempt would take a later sub-batch's
# reserve; sub-batches 2 to 5, whose first attempt fails at the full timeout and whose second
# succeeds, are all forwarded, so no later sub-batch is left without its first attempt and its full
# retry. Run time, on a virtual clock, with a `MockTransport` door: attempt 1 fails at the **full**
# `request_timeout_seconds` (a read timeout), the client waits `retry_backoff_seconds`, attempt 2
# is sent with the full `request_timeout_seconds` available and the door answers `202`: the receipt
# reads forwarded equal to the events handed in, with zero left unconfirmed. The same with 5
# sub-batches in which every sub-batch's first attempt fails at the full timeout: every sub-batch
# gets its second attempt and all are forwarded. A flush whose remaining budget is below the
# backoff plus a full timeout starts no further attempt and reports the rest unconfirmed (no
# attempt is sent with a clipped timeout) -- falsifier: `pytest ... -k https_flush_budget` with the
# rule reverted to `flush_timeout_seconds >= request_timeout_seconds` turns the load cases red and
# the first-run case red (the recovery attempt never runs and the batch ends unconfirmed), with the
# `sub_batches` factor dropped turns the 5-sub-batch case red, and with the retry gate removed (a
# clipped-timeout attempt sent) turns the no-clipped-attempt case red, with the sub-batch count
# taken from `max_batch_records` alone (`ceil(max_events / max_batch_records)`) turns the
# byte-split load case red, and with the later-sub-batch reserve dropped from the retry gate (a
# third attempt on sub-batch 1 sent whenever it fits the budget) turns the reservation case red
# (sub-batch 5 is left without its recovery attempt)
def test_https_flush_budget_load_counts_byte_bound_and_accepts_the_exact_budget() -> (
    None
):
    with pytest.raises(ValidationError, match="max_attempts"):
        overlay(max_attempts=1)

    settings = document(
        max_batch_records=100,
        max_event_bytes=512,
        max_batch_bytes=len(b'{"records":[]}') + 10 * 513,
        request_timeout_seconds=105,
    )
    settings["max_events"] = 500
    batch_budget = 2 * 105 + 2
    for bad in (105, batch_budget, 5 * batch_budget, 50 * batch_budget - 1):
        with pytest.raises(ValidationError, match="flush_timeout_seconds"):
            ModelRuntimeBusForwardOverlay.model_validate(
                settings | {"flush_timeout_seconds": bad}
            )
    ModelRuntimeBusForwardOverlay.model_validate(
        settings | {"flush_timeout_seconds": 50 * batch_budget}
    )


@pytest.mark.parametrize("batch_count", [1, 5])
def test_https_flush_budget_full_retry_for_every_batch(batch_count: int) -> None:
    clock = Clock()
    timeouts = []

    def answer(attempt: int, req: httpx.Request) -> httpx.Response:
        timeout = req.extensions["timeout"]["read"]
        timeouts.append(timeout)
        if attempt % 2 == 1:
            clock.seconds += timeout
            raise httpx.ReadTimeout("full phase timeout")
        return accepted(req)

    config = ModelRuntimeBusForwardOverlay.model_validate(
        document(max_batch_records=1)
        | {"max_events": batch_count, "flush_timeout_seconds": batch_count * 14}
    )
    door = RecordingDoor(answer)
    assert receipt(
        config, door, *(event(bytes([n])) for n in range(batch_count)), clock=clock
    ) == {
        "forwarded": batch_count,
        "failed": 0,
        "detail": None,
    }
    assert timeouts == [6] * (2 * batch_count)
    assert clock.sleeps == [2] * batch_count


def test_https_flush_budget_reserves_the_later_sub_batches() -> None:
    clock = Clock()
    attempted: dict[str, int] = {}

    def answer(_attempt: int, req: httpx.Request) -> httpx.Response:
        value = records(req.content)[0]["value"]
        attempted[value] = attempted.get(value, 0) + 1
        if (value == b64(b"\x00") and attempted[value] < 4) or attempted[value] == 1:
            clock.seconds += req.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("full phase timeout")
        return accepted(req)

    config = ModelRuntimeBusForwardOverlay.model_validate(
        document(max_batch_records=1, max_attempts=4)
        | {"max_events": 5, "flush_timeout_seconds": 78}
    )
    door = RecordingDoor(answer)
    result = receipt(config, door, *(event(bytes([n])) for n in range(5)), clock=clock)
    assert (result["forwarded"], result["failed"]) == (4, 1)
    assert list(attempted.values()) == [3, 2, 2, 2, 2]
    assert all(req.extensions["timeout"]["read"] == 6 for req in door.requests)


def test_https_flush_budget_starts_no_retry_with_less_than_a_full_attempt() -> None:
    clock = Clock()

    def answer(attempt: int, request: httpx.Request) -> httpx.Response:
        if attempt == 1:
            clock.seconds += 6
            raise httpx.ReadTimeout("full phase timeout")
        return accepted(request)

    door = RecordingDoor(answer)
    sender = publisher(overlay(), door=door, clock=clock)
    sender.publish(TOPIC, None, b"payload", {})
    assert sender.flush(13) == 1
    assert len(door.requests) == 1
    assert not clock.sleeps


# - door busy, upload deadline and drift (`-k https_door_busy`): a `MockTransport` door answering
# `503 door-busy` (no body lists) to attempt 1 and `202` to attempt 2 yields forwarded equal to the
# events handed in, with attempt 2 sent after `retry_backoff_seconds` and not after `Retry-After`;
# a `408` is retried the same way; a `500 kafka-limit-drift` answering record 2 of 3 `failed` with
# `retryable: false` after record 1 `published` is sent **once**: forwarded 1, failed 2,
# `failure_detail` naming the drift, no second request -- falsifier: `pytest ... -k
# https_door_busy` with `door-busy` treated as not retryable turns the first case red, and with the
# `retryable: false` flag ignored (the `500` retried) turns the request-count case red
@pytest.mark.parametrize(
    ("status", "reason"), [(503, "door-busy"), (408, "upload-deadline")]
)
def test_https_door_busy_and_upload_deadline_use_the_overlay_backoff(
    status: int, reason: str
) -> None:
    clock = Clock()
    times = []

    def answer(attempt: int, req: httpx.Request) -> httpx.Response:
        times.append(clock.seconds)
        if attempt == 1:
            return httpx.Response(
                status, json={"reason": reason}, headers={"Retry-After": "120"}
            )
        return accepted(req)

    door = RecordingDoor(answer)
    result = receipt(overlay(), door, event(b"first"), event(b"second"), clock=clock)
    assert (result["forwarded"], result["failed"]) == (2, 0)
    assert times == [0, 2]


def test_https_door_busy_kafka_drift_keeps_prior_ack_but_never_retries() -> None:
    door = RecordingDoor(
        lambda _, req: outcome(
            req, 1, status=500, reason="kafka-limit-drift", retryable=False
        )
    )
    result = receipt(
        overlay(), door, event(b"confirmed"), event(b"drift"), event(b"later")
    )
    assert (result["forwarded"], result["failed"], len(door.requests)) == (1, 2, 1)
    assert "kafka-limit-drift" in str(result["detail"])


# - earlier acknowledgments survive a lost retry (review round 5; recorded in the in-flush
# acknowledged set of section 3 Client): a `MockTransport` door answers attempt 1 with a `503`
# listing records 0 and 1 `published`, record 2 `failed` and record 3 `not_attempted` (the shape
# the door really answers: it stops at the first record that is not `published`), then attempt 2
# loses its response (a read timeout) and attempt 3 does too. The receipt reads forwarded 2 and
# failed 2, not forwarded 0; attempts 2 and 3 carried only records 2 and 3 (the door saw no resend
# of 0 or 1); and a second `flush` with no new `publish` posts nothing and holds no set. The same
# with attempt 2 answered `422` for records 2 and 3: forwarded 2, failed 2, no further attempt.
# **Repeated `event_id` (review round 6; a separate fixture from the one above, its totals stand on
# their own):** a batch of 4 positions in which positions 1 and 2 carry the same `event_id` (the
# same topic, key, value bytes and identity headers): the door answers attempt 1 with a `503`,
# positions 0 and 1 `published`, position 2 `failed` and position 3 `not_attempted`. Case A,
# attempt 2 answered: attempt 2 carries positions 2 and 3 only, the stateless door answers `202`
# for both (position 2 is written as a copy of position 1's `event_id`, expected), and the receipt
# reads forwarded 4, failed 0. Case B, attempt 2 and every later attempt lost: the receipt reads
# forwarded 2 (positions 0 and 1) and failed 2 (positions 2 and 3), not forwarded 3 and not
# forwarded 1, so position 2 still counts failed even though its `event_id` equals that of
# acknowledged position 1 -- falsifier: `pytest ... -k https_ack_survives` with the acknowledged
# set reset at the start of each attempt turns the forwarded-count case red (forwarded 0), with the
# set kept after `flush` returns turns the no-hold case red, and with the set keyed by `event_id`
# turns the repeated-id case B red (position 2 counted forwarded after a lost attempt)
@pytest.mark.parametrize("last", ["lost", "refused", "accepted"])
@pytest.mark.parametrize("repeated", [False, True])
def test_https_ack_survives_lost_retry_and_keeps_repeated_ids_independent(
    last: str, repeated: bool
) -> None:
    def answer(attempt: int, req: httpx.Request) -> httpx.Response:
        if attempt == 1:
            return outcome(req, 2)
        if last == "lost":
            raise httpx.ReadTimeout("lost reply")
        if last == "refused":
            return httpx.Response(
                422, json={"refusals": [{"index": 0, "reason": "topic-not-allowed"}]}
            )
        return accepted(req)

    door = RecordingDoor(answer)
    sender = publisher(overlay(max_attempts=3), door=door, clock=Clock())
    first = b"first"
    second = b"repeated"
    third = second if repeated else b"third"
    fourth = b"fourth"
    for value in (first, second, third, fourth):
        sender.publish(TOPIC, None, value, {})
    assert sender.flush(40) == (0 if last == "accepted" else 2)
    assert values(door) == [[first, second, third, fourth]] + (
        [[third, fourth]] * (2 if last == "lost" else 1)
    )
    if repeated:
        wire = records(door.requests[0].content)
        assert wire[1]["event_id"] == wire[2]["event_id"]
    count = len(door.requests)
    assert sender.flush(40) == 0
    assert len(door.requests) == count
    sender.publish(TOPIC, None, second, {})
    assert sender.flush(40) == (1 if last != "accepted" else 0)
    assert values(door)[count:] == [[second]] * (3 if last == "lost" else 1)


# - a transport failure with no body leaves the sub-batch unconfirmed, retries at most
# `max_attempts` times and never past `flush`'s `timeout_s`, and `failure_detail` says it may have
# been published -- falsifier: `pytest ... -k https_transport` with `max_attempts` ignored turns
# the attempt-count case red, and with the `timeout_s` guard removed turns the deadline case red
@pytest.mark.parametrize(("budget", "attempts"), [(100, 3), (13, 1), (14, 2), (5, 0)])
def test_https_transport_loss_obeys_attempt_limit_and_absolute_flush_budget(
    budget: int, attempts: int
) -> None:
    clock = Clock()

    def lost(_attempt: int, req: httpx.Request) -> httpx.Response:
        assert req.extensions["timeout"]["read"] == 6
        clock.seconds += 6
        raise httpx.ConnectError("failure with no body")

    door = RecordingDoor(lost)
    sender = publisher(overlay(max_attempts=3), door=door, clock=clock)
    sender.publish(TOPIC, b"key", b"value", {})
    assert sender.flush(budget) == 1
    assert (len(door.requests), clock.sleeps) == (attempts, [2] * max(0, attempts - 1))
    assert clock.seconds <= budget
    detail = str(sender.failure_detail())
    if attempts:
        assert "may have been published" in detail
    else:
        assert "flush" in detail


# - the retry claim is only what the node does: no event is held after `flush` returns, and a
# second `flush` with no new `publish` posts nothing -- falsifier: `pytest ... -k
# https_no_retention` with a post-failure buffer kept turns it red
def test_https_no_retention_after_a_failed_flush() -> None:
    door = RecordingDoor(
        lambda attempt, request: (
            httpx.Response(503) if attempt == 1 else accepted(request)
        )
    )
    sender = publisher(overlay(), door=door, clock=Clock())
    sender.publish(TOPIC, None, b"old", {})
    assert sender.flush(6) == 1
    assert sender.flush(60) == 0
    sender.publish(TOPIC, None, b"new", {})
    assert sender.flush(60) == 0
    assert values(door) == [[b"old"], [b"new"]]


# - retry recovery with a stateless door (`-k https_retry_recovery`, review round 10 item 1): a
# `MockTransport` door answers attempt 1 with a whole-batch `503` carrying `Retry-After: 120` and
# every later attempt with `202` at once; with a test overlay whose `retry_backoff_seconds` and
# `request_timeout_seconds` are small enough that `flush_timeout_seconds` at the flush budget
# (section 3 Deadline join) is itself under 120, attempt 2 is sent after `retry_backoff_seconds`
# and not after `Retry-After`, the receipt reads forwarded equal to the events handed in, and the
# client held no per-record wait state (no lease timer): a record is never withheld from a retry
# because of an earlier attempt -- falsifier: `pytest ... -k https_retry_recovery` with the client
# waiting out `Retry-After` (a lease-aware wait) turns the flush-budget case red (the flush ends
# with the batch unconfirmed)
def test_https_retry_recovery_resends_the_unconfirmed_records_without_lease_state() -> (
    None
):
    clock = Clock()
    configuration = ModelRuntimeBusForwardOverlay.model_validate(
        document() | {"max_events": 4, "flush_timeout_seconds": 14}
    )
    door = RecordingDoor(
        lambda attempt, req: (
            httpx.Response(503, headers={"Retry-After": "120"})
            if attempt == 1
            else accepted(req)
        )
    )
    result = receipt(configuration, door, event(b"one"), event(b"two"), clock=clock)
    assert result == {"forwarded": 2, "failed": 0, "detail": None}
    assert values(door) == [[b"one", b"two"], [b"one", b"two"]]
    assert clock.sleeps == [2]


# - an overlay whose `ingest_url_ref` or `auth_ref` is a literal URL or token is refused at load,
# and an absent key refuses the load (no defaults), `max_attempts`, `retry_backoff_seconds`,
# `max_event_bytes`, `max_batch_bytes`, `max_authorization_bytes` and the six field limits
# included, so a proof overlay that omits any one fails to load -- falsifier: `pytest ... -k
# https_overlay` with `extra=forbid` removed turns the unknown-key case red and with `max_attempts`
# or `max_batch_bytes` given a default turns the matching absent-key case red
def test_https_overlay_rejects_literals_unknown_fields_and_every_missing_field() -> (
    None
):
    assert overlay().event_bus.type == "https"
    for key, value in (
        ("ingest_url_ref", URL),
        ("auth_ref", "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.fixture"),
        ("auth_ref", "synthetic-bearer-token"),
        ("unknown", True),
    ):
        with pytest.raises(ValidationError, match=key):
            overlay(**{key: value})
    for key in document()["event_bus"]:
        data = document()
        del data["event_bus"][key]
        with pytest.raises(ValidationError, match=key):
            ModelRuntimeBusForwardOverlay.model_validate(data)


# - Each attempt runs under a wall-clock deadline (not only HTTPX per-phase timeouts) that fits
# inside the flush budget; a test with a trickling slow response shows a stalled request ends at
# its deadline and later batches keep their reserved first attempt and retry.
# Falsifier: remove the deadline and use HTTPX per-phase timeouts alone. The trickling response
# outlives the attempt and a subsequent sub-batch loses its reserve.
def test_https_wall_clock_deadline_cancels_trickling_body_and_reserves_later_attempts(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    request_timeout = 0.5
    flush_budget = 2.4
    starts: list[tuple[bytes, float]] = []

    def answer(attempt: int, handler: StreamRequestHandler, body: bytes) -> None:
        value = base64.b64decode(records(body)[0]["value"])
        starts.append((value, time.monotonic()))
        if attempt == 1:
            # Every read makes progress sooner than the phase timeout. The complete
            # body takes 2.5 seconds, longer than both the attempt and flush budgets.
            handler.wfile.write(
                b"HTTP/1.1 503 test\r\nContent-Length: 50\r\nConnection: close\r\n\r\n"
            )
            try:
                for _ in range(50):
                    handler.wfile.write(b" ")
                    time.sleep(0.05)
            except OSError:
                pass  # The deadline closes the connection while bytes are still arriving.
        else:
            respond(handler, 503 if attempt == 3 else 202, b'{"published":1}')

    configuration = ModelRuntimeBusForwardOverlay.model_validate(
        document(
            max_batch_records=1,
            request_timeout_seconds=request_timeout,
            door_record_confirm_timeout_seconds=0.05,
            request_margin_seconds=0.05,
            retry_backoff_seconds=0.05,
        )
        | {"max_events": 2, "flush_timeout_seconds": flush_budget}
    )
    with loopback_door(tmp_path, answer) as (url, verify, observed):
        sender = publisher(
            configuration,
            url=url,
            transport_factory=lambda: httpx.AsyncHTTPTransport(verify=verify),
        )
        sender.publish(TOPIC, None, b"early", {})
        sender.publish(TOPIC, None, b"later", {})
        start = time.monotonic()
        unconfirmed = sender.flush(flush_budget)
        elapsed = time.monotonic() - start
        observed_attempts = len(observed)
    sequence = [value for value, _ in starts]
    record_property("flush_elapsed_ms", round(elapsed * 1000))
    record_property(
        "attempts_per_batch", f"{sequence.count(b'early')},{sequence.count(b'later')}"
    )
    record_property("unconfirmed", unconfirmed)
    assert unconfirmed == 0
    assert observed_attempts == 4
    assert sequence == [b"early", b"early", b"later", b"later"]
    delay = starts[1][1] - starts[0][1]
    record_property("early_retry_delay_ms", round(delay * 1000))
    assert 0.4 <= delay < 1.0
    assert elapsed <= flush_budget
