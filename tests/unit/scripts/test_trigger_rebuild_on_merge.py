# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/trigger_rebuild_on_merge.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "trigger_rebuild_on_merge.py"


def _load_trigger_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "trigger_rebuild_on_merge", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trigger_rebuild_on_merge"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trigger_module() -> object:
    return _load_trigger_module()


class _FakeMessage:
    def __init__(self, payload: dict[str, Any], error: object | None = None) -> None:
        self._payload = payload
        self._error = error

    def error(self) -> object | None:
        return self._error

    def value(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _FakeConsumer:
    last: _FakeConsumer | None = None

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.closed = False
        self.subscriptions: list[list[str]] = []
        self.messages = [
            _FakeMessage({"correlation_id": "other", "status": "success"}),
            _FakeMessage({"correlation_id": "corr-123", "status": "success"}),
        ]
        _FakeConsumer.last = self

    def subscribe(self, topics: list[str]) -> None:
        self.subscriptions.append(topics)

    def poll(self, _timeout: float) -> _FakeMessage | None:
        if self.messages:
            return self.messages.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


@pytest.mark.unit
def test_wait_for_rebuild_completion_matches_correlation_and_closes(
    trigger_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_confluent = types.SimpleNamespace(Consumer=_FakeConsumer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    completion = trigger_module.wait_for_rebuild_completion(  # type: ignore[attr-defined]
        bootstrap_servers="broker:9092",
        username="user",
        password="secret",
        correlation_id="corr-123",
        timeout_seconds=5,
    )

    assert completion["correlation_id"] == "corr-123"
    assert completion["status"] == "success"
    assert _FakeConsumer.last is not None
    assert _FakeConsumer.last.closed is True
    assert _FakeConsumer.last.subscriptions == [
        ["onex.evt.deploy.rebuild-completed.v1"]
    ]
    assert _FakeConsumer.last.config["group.id"] == (
        "gha-runtime-rebuild-trigger-corr-123"
    )


@pytest.mark.unit
def test_wait_for_rebuild_completion_times_out_and_closes(
    trigger_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoMessageConsumer(_FakeConsumer):
        def __init__(self, config: dict[str, object]) -> None:
            super().__init__(config)
            self.messages = []

    fake_confluent = types.SimpleNamespace(Consumer=_NoMessageConsumer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    with pytest.raises(TimeoutError, match="Timed out after 0s"):
        trigger_module.wait_for_rebuild_completion(  # type: ignore[attr-defined]
            bootstrap_servers="broker:9092",
            username="user",
            password="secret",
            correlation_id="corr-123",
            timeout_seconds=0,
        )

    assert _FakeConsumer.last is not None
    assert _FakeConsumer.last.closed is True


@pytest.mark.unit
def test_cli_wait_for_completion_fails_on_failed_completion(
    trigger_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "user")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "secret")
    monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", "hmac-secret")
    monkeypatch.setattr(trigger_module, "publish_rebuild_event", lambda **_kwargs: None)
    monkeypatch.setattr(
        trigger_module,
        "wait_for_rebuild_completion",
        lambda **_kwargs: {
            "correlation_id": "corr-123",
            "status": "failed",
            "errors": ["bad deploy"],
        },
    )

    result = CliRunner().invoke(
        trigger_module.main,  # type: ignore[attr-defined]
        [
            "--changed-files",
            "src/omnimarket/nodes/node_runtime_sweep/handler.py",
            "--correlation-id",
            "corr-123",
            "--wait-for-completion",
        ],
    )

    assert result.exit_code == 1
    assert "Received rebuild-completed status=failed" in result.output
    assert "bad deploy" in result.output
