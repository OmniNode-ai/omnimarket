# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of the handlers and stores without the bus."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    FileDevRollArtifactStore,
    InMemoryDevRollArtifactStore,
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollOutcome,
    EnumDevRollUnknownReason,
    ModelDevRollCapturePolicy,
    ModelDevRollFileStore,
    ModelDevRollIncidentEvent,
    ModelDevRollManifestEvent,
    ModelDevRollTerminal,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.conftest import (
    NOW,
    POLICY,
    incident_payload,
    manifest_payload,
    write_overlay,
)

pytestmark = pytest.mark.unit


def _incident_handler(store, clock: datetime = NOW) -> HandlerDevRollIncidentCapture:
    return HandlerDevRollIncidentCapture(store, policy=POLICY, clock=lambda: clock)


def _manifest_handler(store, clock: datetime = NOW) -> HandlerDevRollManifestRecord:
    return HandlerDevRollManifestRecord(store, policy=POLICY)


def test_a_capture_longer_than_max_log_bytes_keeps_the_last_bytes() -> None:
    store = InMemoryDevRollArtifactStore()
    handler = _incident_handler(store)
    log = "x" * 2048
    incident = incident_payload()
    capture = dict(incident["log_captures"][0])
    capture["log"] = log
    capture["bytes_total"] = 2048
    capture["bytes_kept"] = 1024
    capture["complete"] = True
    incident["log_captures"] = [capture]
    terminal = handler.handle(ModelDevRollIncidentEvent.model_validate(incident))
    assert isinstance(terminal, ModelDevRollTerminal)
    (artifact,) = terminal.log_artifacts
    assert artifact.truncated is True
    assert artifact.complete is False
    assert artifact.bytes_kept == 1024
    stored = store.read(artifact.artifact_key)
    assert stored is not None
    assert stored == log[-1024:].encode("utf-8")


def test_a_source_incomplete_capture_is_written_but_incomplete() -> None:
    store = InMemoryDevRollArtifactStore()
    handler = _incident_handler(store)
    incident = incident_payload()
    capture = dict(incident["log_captures"][0])
    capture["complete"] = False
    incident["log_captures"] = [capture]
    terminal = handler.handle(ModelDevRollIncidentEvent.model_validate(incident))
    assert isinstance(terminal, ModelDevRollTerminal)
    (artifact,) = terminal.log_artifacts
    assert artifact.written is True
    assert artifact.complete is False


def test_a_manifest_issuer_differing_from_the_incident_is_recovered() -> None:
    store = InMemoryDevRollArtifactStore()
    manifest_handler = _manifest_handler(store)
    incident_handler = _incident_handler(store)
    manifest_handler.handle(
        ModelDevRollManifestEvent.model_validate(
            manifest_payload(issuer="operator-lane")
        )
    )
    terminal = incident_handler.handle(
        ModelDevRollIncidentEvent.model_validate(
            incident_payload(issuer="another-lane")
        )
    )
    assert isinstance(terminal, ModelDevRollTerminal)
    assert terminal.outcome is EnumDevRollOutcome.RECOVERED
    assert terminal.manifest_issuer_matches is False


def test_the_file_store_writes_atomically_and_reads_back(tmp_path: Path) -> None:
    store = FileDevRollArtifactStore(tmp_path)
    digest = store.write("logs/a.log", b"hello")
    assert store.read("logs/a.log") == b"hello"
    import hashlib

    assert digest == hashlib.sha256(b"hello").hexdigest()


def test_the_file_store_refuses_a_hostile_key(tmp_path: Path) -> None:
    store = FileDevRollArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="artifact key"):
        store.write("../x", b"data")
    with pytest.raises(ValueError, match="artifact key"):
        store.write("/abs", b"data")


def test_the_file_store_reads_a_missing_key_as_none(tmp_path: Path) -> None:
    store = FileDevRollArtifactStore(tmp_path)
    assert store.read("absent") is None


def test_store_from_policy_with_the_home_env_missing_raises_node_overlay_error() -> (
    None
):
    policy = ModelDevRollCapturePolicy(
        store=ModelDevRollFileStore(
            backend="file", home_env="EXAMPLE_STATE_DIR", relpath="dev-roll-capture"
        ),
        max_log_bytes=1024,
    )
    with pytest.raises(NodeOverlayError) as excinfo:
        store_from_policy(policy, {})
    assert "EXAMPLE_STATE_DIR" in str(excinfo.value)


def test_the_overlay_loader_refuses_an_extra_key(tmp_path: Path) -> None:
    doc = {
        "store": {"backend": "in_memory"},
        "max_log_bytes": 1024,
        "unknown": 1,
    }
    environ = write_overlay(tmp_path, doc)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
        load_capture_policy,
    )

    with pytest.raises(NodeOverlayError):
        load_capture_policy(environ)


def test_the_overlay_loader_refuses_a_zero_max_log_bytes(tmp_path: Path) -> None:
    doc = {
        "store": {"backend": "in_memory"},
        "max_log_bytes": 0,
    }
    environ = write_overlay(tmp_path, doc)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
        load_capture_policy,
    )

    with pytest.raises(NodeOverlayError):
        load_capture_policy(environ)


def test_the_terminal_model_refuses_an_unknown_reason_on_recovered() -> None:
    with pytest.raises(ValidationError):
        ModelDevRollTerminal(
            terminal_id=uuid4(),
            job_id="job-1",
            issuer="operator-lane",
            outcome=EnumDevRollOutcome.RECOVERED,
            unknown_reason=EnumDevRollUnknownReason.ISSUER_MISSING,
            detail=None,
            observed_at=NOW,
            incident_at=None,
            commit=None,
            error=None,
            recovery_verified=True,
            manifest_seen=True,
            manifest_issuer_matches=True,
            health_probe_count=1,
            log_artifacts=(),
            terminal_artifact_key=None,
        )


def test_the_terminal_model_refuses_a_missing_reason_on_unknown() -> None:
    with pytest.raises(ValidationError):
        ModelDevRollTerminal(
            terminal_id=uuid4(),
            job_id="job-1",
            issuer="operator-lane",
            outcome=EnumDevRollOutcome.UNKNOWN,
            unknown_reason=None,
            detail=None,
            observed_at=NOW,
            incident_at=None,
            commit=None,
            error=None,
            recovery_verified=None,
            manifest_seen=False,
            manifest_issuer_matches=None,
            health_probe_count=0,
            log_artifacts=(),
            terminal_artifact_key=None,
        )
