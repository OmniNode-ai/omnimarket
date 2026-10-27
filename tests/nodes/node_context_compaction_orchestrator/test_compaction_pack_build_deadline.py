# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction keeps one deadline across publishing and reply delivery."""

from __future__ import annotations

import logging

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.event_bus.event_bus_semantic_fake import EventBusSemanticFake

from .test_compaction_pack_build_barrier import (
    _binding,
    _Clock,
    _PackOrchestrator,
    _probe,
)
from .test_session_barrier_wait import _request, _Source

pytestmark = pytest.mark.unit


@pytest.fixture(
    params=[EventBusInmemory, EventBusSemanticFake],
    ids=["inmemory", "broker-semantics"],
)
def deadline_bus(request, compaction_bus):
    return compaction_bus(_PackOrchestrator(), bus_class=request.param)


def _warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "context compaction fell back" in record.getMessage()
    ]


def test_compaction_pack_build_barrier_publish_consumes_reply_budget(deadline_bus):
    clock = _Clock()
    publish = deadline_bus.publisher

    def slow_publish(topic, payload):
        clock.sleep(0.75)
        publish(topic, payload)

    result = deadline_bus.handler(
        binding=_binding(), monotonic=clock.monotonic, event_publisher=slow_publish
    ).handle(_request())
    assert result.reply.status == "built"
    assert len(deadline_bus.commands) == 1
    assert deadline_bus.consumer.waits[0][1] == 0.5


@pytest.mark.parametrize("phase", ["publish", "reply"])
def test_compaction_pack_build_barrier_late_package_falls_back_once(
    deadline_bus, caplog, phase
):
    clock = _Clock()
    publish = deadline_bus.publisher
    open_session = deadline_bus.consumer.open
    publications = []

    def slow_publish(topic, payload):
        publications.append((topic, payload))
        if phase == "publish":
            clock.sleep(2.0)
        publish(topic, payload)

    def slow_session(topic):
        session = open_session(topic)
        wait = session.wait

        def slow_wait(correlation_id, timeout_seconds):
            reply = wait(correlation_id, timeout_seconds)
            if phase == "reply":
                clock.sleep(2.0)
            return reply

        session.wait = slow_wait
        return session

    deadline_bus.consumer.open = slow_session
    request = _request()
    result = deadline_bus.handler(
        binding=_binding(), monotonic=clock.monotonic, event_publisher=slow_publish
    ).handle(request)
    assert result.reply.status == "native"
    assert result.reply.package_id is None
    assert result.prepared is None
    assert result.activated is None
    assert result.fallback.failure_class.value == "pack_builder_unavailable"
    assert len(publications) == 1
    (warning,) = _warnings(caplog)
    assert str(request.invocation_id) in warning.getMessage()
    if phase == "publish":
        assert deadline_bus.consumer.waits == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("session_id", "other-session"), ("barrier_id", "other-barrier")],
)
def test_compaction_pack_build_barrier_revision_identity_must_match(
    deadline_bus, caplog, field, value
):
    met = _probe((1, 2, 3), (1, 2, 3))
    source = _Source([met, met.model_copy(update={field: value})])
    request = _request()
    result = deadline_bus.handler(
        binding=_binding(), barrier_probe_source=source
    ).handle(request)
    assert result.reply.status == "native"
    assert result.fallback.failure_class.value == "build_error"
    assert not deadline_bus.commands
    assert result.prepared is result.activated is None
    assert len(_warnings(caplog)) == 1


def test_compaction_pack_build_barrier_subscription_expiry_does_not_publish(
    deadline_bus, caplog
):
    clock = _Clock()
    open_session = deadline_bus.consumer.open
    publications = []
    closed = []

    def slow_session(topic):
        session = open_session(topic)
        clock.sleep(0.75)
        session.close = lambda: closed.append(topic)
        return session

    deadline_bus.consumer.open = slow_session
    result = deadline_bus.handler(
        binding=_binding(),
        monotonic=clock.monotonic,
        event_publisher=lambda topic, payload: publications.append((topic, payload)),
    ).handle(_request())
    assert result.reply.status == "native"
    assert result.fallback.failure_class.value == "pack_builder_unavailable"
    assert publications == []
    assert deadline_bus.consumer.waits == []
    assert closed == [
        deadline_bus.topics.failed_topic,
        deadline_bus.topics.completed_topic,
    ]
    assert len(_warnings(caplog)) == 1
