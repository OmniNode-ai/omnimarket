# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDevRollIncidentCapture (OMN-24013)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.handler_dev_roll_incident_capture import (
    HandlerDevRollIncidentCapture,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollCaptureFailure,
    ModelDevLogCapture,
    ModelDevRollCaptureFailed,
    ModelDevRollIncident,
    ModelDevRollTerminalRecorded,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    ModelDevRollCaptureSettings,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols.protocol_roll_artifact_store import (
    RollArtifactStoreError,
)

pytestmark = pytest.mark.unit


def _make_settings(log_byte_cap: int = 1024) -> ModelDevRollCaptureSettings:
    return ModelDevRollCaptureSettings(
        artifact_root="/tmp/artifacts",
        retention_days=7,
        log_byte_cap=log_byte_cap,
    )


def _make_incident(**overrides: Any) -> ModelDevRollIncident:
    defaults: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "job_id": "job-123",
        "issuer": "deploy-agent",
        "commit": "abc123",
        "error": "container crashed",
        "project": "my-project",
        "at": datetime.now(timezone.utc),
        "restored_revision": "def456",
        "recovery_verified": True,
        "recovery_skipped_healthy": None,
        "recovery_error": None,
        "runtime_kept_revision": None,
        "health_probes": [],
        "log_captures": [],
    }
    defaults.update(overrides)
    return ModelDevRollIncident(**defaults)


def _make_log_capture(**overrides: Any) -> ModelDevLogCapture:
    defaults: dict[str, Any] = {
        "service": "api",
        "container": "api-1",
        "bytes_total": 100,
        "bytes_kept": 100,
        "complete": True,
        "redacted": False,
        "sha256": "a" * 64,
        "artifact_path": "job-123/api.log",
        "error": None,
        "log": "log content here",
    }
    defaults.update(overrides)
    return ModelDevLogCapture(**defaults)


class TestHandlerDevRollIncidentCapture:
    def test_recovery_verified_no_error_gives_restored_verified(self) -> None:
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident(recovery_verified=True, recovery_error=None)
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert result.outcome.value == "restored_verified"

    def test_recovery_not_verified_no_error_gives_restored_unverified(self) -> None:
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident(recovery_verified=False, recovery_error=None)
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert result.outcome.value == "restored_unverified"

    def test_recovery_error_set_gives_recovery_failed(self) -> None:
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident(recovery_verified=False, recovery_error="timeout")
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert result.outcome.value == "recovery_failed"

    def test_failing_control_recovery_verified_false_with_error_never_restored_verified(self) -> None:
        """Failing control: recovery_verified False + recovery_error set must give recovery_failed, never restored_verified."""
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident(recovery_verified=False, recovery_error="something broke")
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert result.outcome.value == "recovery_failed"
        assert result.outcome.value != "restored_verified"

    def test_store_failure_on_terminal_json_yields_capture_failed(self) -> None:
        """Failing control: store failure on terminal JSON yields ModelDevRollCaptureFailed, never a terminal."""
        store = MagicMock()
        store.write.side_effect = RollArtifactStoreError("disk full")
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident()
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollCaptureFailed)
        assert result.reason == EnumDevRollCaptureFailure.STORE_UNWRITABLE

    def test_log_longer_than_cap_stored_cut_with_complete_false(self) -> None:
        """A log longer than log_byte_cap is stored cut with complete False."""
        store = MagicMock()
        store.write.return_value = "memory://ref"
        settings = _make_settings(log_byte_cap=10)
        handler = HandlerDevRollIncidentCapture(store=store, settings=settings)
        long_log = "x" * 100
        log_capture = _make_log_capture(log=long_log, bytes_total=100, bytes_kept=10)
        incident = _make_incident(log_captures=[log_capture])
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert not result.capture_complete

    def test_capture_complete_when_all_logs_complete_and_not_redacted(self) -> None:
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        log_capture = _make_log_capture(complete=True, redacted=False)
        incident = _make_incident(log_captures=[log_capture], health_probes=[{"service": "api"}])
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert result.capture_complete

    def test_no_log_captures_with_health_probes_not_complete(self) -> None:
        store = MagicMock()
        store.write.return_value = "memory://ref"
        handler = HandlerDevRollIncidentCapture(store=store, settings=_make_settings())
        incident = _make_incident(log_captures=[], health_probes=[{"service": "api"}])
        result = asyncio.run(handler.handle(incident))
        assert isinstance(result, ModelDevRollTerminalRecorded)
        assert not result.capture_complete
