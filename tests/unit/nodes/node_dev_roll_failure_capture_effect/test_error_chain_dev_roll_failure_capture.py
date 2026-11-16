# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_failure_capture_effect: the same runtime path, a failing input each."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollOutcome,
    EnumDevRollUnknownReason,
    ModelDevRollTerminal,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.chain_support import (
    CLOCK,
    INCIDENT_TOPIC,
    KEY,
    MANIFEST_TOPIC,
    RECORDED_TOPIC,
    TERMINAL_TOPIC,
    chain,
    chain_with,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.conftest import (
    incident_payload,
    manifest_payload,
)

pytestmark = pytest.mark.unit


class _UnwritableStore:
    """A store whose write always raises OSError."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def write(self, key: str, content: bytes) -> str:
        raise OSError("the store is unwritable")

    def read(self, key: str) -> bytes | None:
        return self._data.get(key)


class _TerminalUnwritableStore:
    """A store that accepts log writes but raises on the terminal key."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def write(self, key: str, content: bytes) -> str:
        if key.startswith("terminals/"):
            raise OSError("the terminal store is unwritable")
        self._data[key] = content
        return "0" * 64

    def read(self, key: str) -> bytes | None:
        return self._data.get(key)


async def _send(store, manifest: dict[str, object], incident: dict[str, object]):
    dispatch, transport, broker, _topics = chain(store)
    correlation_id = uuid4()
    await transport.send(
        MANIFEST_TOPIC,
        key=KEY,
        value=envelope(manifest, correlation_id),
        headers={},
    )
    await dispatch.drain()
    await transport.send(
        INCIDENT_TOPIC,
        key=KEY,
        value=envelope(incident, correlation_id),
        headers={},
    )
    await dispatch.drain()
    return broker, correlation_id


def _terminal(broker) -> ModelDevRollTerminal:
    (out,) = on(broker, TERMINAL_TOPIC)
    return ModelDevRollTerminal.model_validate(out.payload)


async def test_an_incident_with_no_issuer_is_unknown_issuer_missing(store) -> None:
    incident = incident_payload()
    del incident["issuer"]
    broker, _cid = await _send(store, manifest_payload(), incident)
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.ISSUER_MISSING


async def test_a_preflight_incident_with_no_issuer_is_unknown_issuer_missing_and_keeps_failed_phase(
    store,
) -> None:
    incident = incident_payload(
        issuer=None, failed_phase="preflight", recovery_verified=False, log_captures=[]
    )
    broker, _cid = await _send(store, manifest_payload(), incident)
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.unknown_reason is EnumDevRollUnknownReason.ISSUER_MISSING
    assert terminal.failed_phase == "preflight"


async def test_an_incident_with_an_empty_issuer_is_unknown_issuer_missing(
    store,
) -> None:
    broker, _cid = await _send(store, manifest_payload(), incident_payload(issuer=""))
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.ISSUER_MISSING


async def test_a_log_capture_missing_sha256_is_unknown_incident_unreadable(
    store,
) -> None:
    incident = incident_payload()
    capture = dict(incident["log_captures"][0])
    del capture["sha256"]
    incident["log_captures"] = [capture]
    broker, _cid = await _send(store, manifest_payload(), incident)
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.INCIDENT_UNREADABLE


async def test_an_incident_missing_recovery_verified_is_unknown_incident_unreadable(
    store,
) -> None:
    incident = incident_payload()
    del incident["recovery_verified"]
    broker, _cid = await _send(store, manifest_payload(), incident)
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.INCIDENT_UNREADABLE


async def test_an_unwritable_store_is_unknown_artifact_store_unwritable() -> None:
    store = _UnwritableStore()
    broker, _cid = await _send(store, manifest_payload(), incident_payload())
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.ARTIFACT_STORE_UNWRITABLE
    for artifact in terminal.log_artifacts:
        assert artifact.written is False


async def test_a_terminal_unwritable_store_is_unknown_with_no_terminal_key() -> None:
    store = _TerminalUnwritableStore()
    broker, _cid = await _send(store, manifest_payload(), incident_payload())
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.ARTIFACT_STORE_UNWRITABLE
    assert terminal.terminal_artifact_key is None


async def test_an_unreadable_overlay_is_unknown_overlay_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Handlers built the way the runtime builds them (no store, no policy) over an empty overlay root.
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty_root))
    dispatch, transport, broker, _topics = chain_with(
        HandlerDevRollManifestRecord(),
        HandlerDevRollIncidentCapture(clock=lambda: CLOCK),
    )
    correlation_id = uuid4()
    await transport.send(
        MANIFEST_TOPIC,
        key=KEY,
        value=envelope(manifest_payload(), correlation_id),
        headers={},
    )
    await transport.send(
        INCIDENT_TOPIC,
        key=KEY,
        value=envelope(incident_payload(), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 2
    (recorded,) = on(broker, RECORDED_TOPIC)
    assert recorded.payload["recorded"] is False
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.OVERLAY_UNREADABLE
    assert terminal.log_artifacts == ()


async def test_a_manifest_with_no_issuer_is_not_recorded_and_writes_nothing(
    store,
) -> None:
    manifest = manifest_payload()
    del manifest["issuer"]
    dispatch, transport, broker, _topics = chain(store)
    correlation_id = uuid4()
    await transport.send(
        MANIFEST_TOPIC,
        key=KEY,
        value=envelope(manifest, correlation_id),
        headers={},
    )
    await dispatch.drain()
    (recorded,) = on(broker, RECORDED_TOPIC)
    assert recorded.payload["recorded"] is False
    assert "issuer" in (recorded.payload["error"] or "")
    assert store.read("manifests/job-1.json") is None


async def test_a_hostile_job_id_is_unknown_incident_unreadable_and_writes_nothing(
    store,
) -> None:
    broker, _cid = await _send(
        store, manifest_payload(), incident_payload(job_id="../x")
    )
    terminal = _terminal(broker)
    assert terminal.outcome is EnumDevRollOutcome.UNKNOWN
    assert terminal.outcome is not EnumDevRollOutcome.RECOVERED
    assert terminal.unknown_reason is EnumDevRollUnknownReason.INCIDENT_UNREADABLE
    assert terminal.log_artifacts == ()
    assert terminal.terminal_artifact_key is None
    assert store.read("incidents/job-1/omnimarket-omnimarket-d.log") is None
