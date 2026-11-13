# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_failure_capture_effect over its real contract on the in-memory bus."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollOutcome,
    ModelDevRollTerminal,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.chain_support import (
    INCIDENT_TOPIC,
    KEY,
    MANIFEST_TOPIC,
    RECORDED_TOPIC,
    TERMINAL_TOPIC,
    chain,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.conftest import (
    incident_payload,
    manifest_payload,
)

pytestmark = pytest.mark.unit


async def _send_manifest_and_incident(
    store, manifest: dict[str, object], incident: dict[str, object]
):
    dispatch, transport, broker, _topics = chain(store)
    correlation_id = uuid4()
    await transport.send(
        MANIFEST_TOPIC,
        key=KEY,
        value=envelope(manifest, correlation_id),
        headers={},
    )
    # The manifest is published at the start of the roll, the incident at its end: read the manifest first.
    assert await dispatch.drain() == 1
    await transport.send(
        INCIDENT_TOPIC,
        key=KEY,
        value=envelope(incident, correlation_id),
        headers={},
    )
    return dispatch, broker, correlation_id


def test_the_subscribed_topics_are_the_contracts_two(store) -> None:
    _dispatch, _transport, _broker, topics = chain(store)
    assert topics == {MANIFEST_TOPIC, INCIDENT_TOPIC}


async def test_golden_recovered_roll_is_published_as_recovered(store) -> None:
    dispatch, broker, correlation_id = await _send_manifest_and_incident(
        store, manifest_payload(), incident_payload()
    )
    assert await dispatch.drain() == 1
    (recorded,) = on(broker, RECORDED_TOPIC)
    assert recorded.payload["recorded"] is True
    assert recorded.payload["issuer"] == "operator-lane"
    (terminal,) = on(broker, TERMINAL_TOPIC)
    assert terminal.correlation_id == correlation_id
    model = ModelDevRollTerminal.model_validate(terminal.payload)
    assert model.outcome is EnumDevRollOutcome.RECOVERED
    assert model.issuer == "operator-lane"
    assert model.manifest_seen is True
    assert model.manifest_issuer_matches is True
    (artifact,) = model.log_artifacts
    assert artifact.complete is True
    assert artifact.written is True
    assert model.terminal_artifact_key is not None
    assert store.read(model.terminal_artifact_key) is not None
    assert store.read(artifact.artifact_key) is not None


async def test_golden_preflight_incident_carries_failed_phase_into_the_terminal(
    store,
) -> None:
    dispatch, broker, _cid = await _send_manifest_and_incident(
        store,
        manifest_payload(),
        incident_payload(
            failed_phase="preflight",
            recovery_verified=False,
            log_captures=[],
            recovery_error="no recovery ran: the runtime was untouched",
        ),
    )
    assert await dispatch.drain() == 1
    (terminal,) = on(broker, TERMINAL_TOPIC)
    model = ModelDevRollTerminal.model_validate(terminal.payload)
    assert model.outcome is EnumDevRollOutcome.UNRECOVERED
    assert model.issuer == "operator-lane"
    assert model.failed_phase == "preflight"


async def test_golden_unrecovered_roll_is_published_as_unrecovered(store) -> None:
    dispatch, broker, _cid = await _send_manifest_and_incident(
        store, manifest_payload(), incident_payload(recovery_verified=False)
    )
    assert await dispatch.drain() == 1
    (terminal,) = on(broker, TERMINAL_TOPIC)
    model = ModelDevRollTerminal.model_validate(terminal.payload)
    assert model.outcome is EnumDevRollOutcome.UNRECOVERED


async def test_an_incident_with_no_manifest_is_recovered_without_a_manifest(
    store,
) -> None:
    dispatch, broker, _cid = await _send_manifest_and_incident(
        store, {}, incident_payload()
    )
    assert await dispatch.drain() == 1
    (terminal,) = on(broker, TERMINAL_TOPIC)
    model = ModelDevRollTerminal.model_validate(terminal.payload)
    assert model.outcome is EnumDevRollOutcome.RECOVERED
    assert model.manifest_seen is False
    assert model.manifest_issuer_matches is None


async def test_the_same_incident_twice_yields_one_terminal_copy(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    correlation_id = uuid4()
    incident = incident_payload()
    for _ in range(2):
        await transport.send(
            INCIDENT_TOPIC,
            key=KEY,
            value=envelope(incident, correlation_id),
            headers={},
        )
    assert await dispatch.drain() == 2
    terminals = on(broker, TERMINAL_TOPIC)
    assert len(terminals) == 2
    first = ModelDevRollTerminal.model_validate(terminals[0].payload)
    second = ModelDevRollTerminal.model_validate(terminals[1].payload)
    assert first.terminal_id == second.terminal_id
    key = first.terminal_artifact_key
    assert key is not None
    assert store.read(key) is not None
