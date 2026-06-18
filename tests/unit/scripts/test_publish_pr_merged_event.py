# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/publish_pr_merged_event.py (OMN-13226)."""

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
SCRIPT_PATH = REPO_ROOT / "scripts" / "publish_pr_merged_event.py"


def _load_publisher_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "publish_pr_merged_event", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["publish_pr_merged_event"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module  # type: ignore[return-value]


@pytest.fixture
def publisher_module() -> types.ModuleType:
    return _load_publisher_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, err: object = None) -> None:
        self._err = err

    def error(self) -> object:
        return self._err


class _FakeProducer:
    """Minimal confluent_kafka.Producer stand-in."""

    instances: list[_FakeProducer] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.produced: list[dict[str, Any]] = []
        _FakeProducer.instances.append(self)
        self._on_delivery: Any = None

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        on_delivery: Any = None,
    ) -> None:
        self._on_delivery = on_delivery
        self.produced.append(
            {
                "topic": topic,
                "key": key.decode("utf-8"),
                "value": json.loads(value.decode("utf-8")),
            }
        )
        # Simulate successful delivery
        if on_delivery is not None:
            on_delivery(None, _FakeMessage())

    def flush(self, timeout: float = 30.0) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_producer_instances() -> None:  # type: ignore[return]
    _FakeProducer.instances.clear()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_payload_shape(publisher_module: types.ModuleType) -> None:
    """build_payload returns all required fields with correct types."""
    payload = publisher_module.build_payload(  # type: ignore[attr-defined]
        repo="OmniNode-ai/omnimarket",
        branch="jonahgabriel/omn-12345-some-feature",
        pr_number=42,
        ticket="OMN-12345",
        merged_at="2026-06-18T12:00:00Z",
        event_id="test-uuid",
    )

    assert payload["topic"] == "onex.evt.github.pr-merged.v1"
    assert payload["repo"] == "OmniNode-ai/omnimarket"
    assert payload["branch"] == "jonahgabriel/omn-12345-some-feature"
    assert payload["pr_number"] == 42
    assert payload["ticket"] == "OMN-12345"
    assert payload["merged_at"] == "2026-06-18T12:00:00Z"
    assert payload["event_id"] == "test-uuid"
    assert "published_at" in payload


@pytest.mark.unit
def test_extract_ticket_from_branch(publisher_module: types.ModuleType) -> None:
    """_extract_ticket pulls OMN-XXXX from a branch name."""
    fn = publisher_module._extract_ticket  # type: ignore[attr-defined]
    assert fn("jonahgabriel/omn-13226-t2-publisher") == "OMN-13226"
    assert fn("jonahgabriel/omn-99-fix") == "OMN-99"
    assert fn("no-ticket-here") == ""


@pytest.mark.unit
def test_extract_ticket_falls_back_to_title(publisher_module: types.ModuleType) -> None:
    """_extract_ticket uses title when branch has no ticket."""
    fn = publisher_module._extract_ticket  # type: ignore[attr-defined]
    assert fn("feature/no-ticket", "feat(OMN-9999): add something") == "OMN-9999"


@pytest.mark.unit
def test_publish_pr_merged_event_correct_topic_and_payload(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish_pr_merged_event sends to the canonical topic with expected payload."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    event_id = publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
        bootstrap_servers="broker:9092",
        username="user",
        password="secret",
        repo="OmniNode-ai/omnimarket",
        branch="jonahgabriel/omn-13226-t2",
        pr_number=99,
        ticket="OMN-13226",
        merged_at="2026-06-18T10:00:00Z",
    )

    assert len(_FakeProducer.instances) == 1
    producer = _FakeProducer.instances[0]
    assert len(producer.produced) == 1

    record = producer.produced[0]
    assert record["topic"] == "onex.evt.github.pr-merged.v1"
    assert record["key"] == "pr-merged/OmniNode-ai/omnimarket/99"

    value = record["value"]
    assert value["repo"] == "OmniNode-ai/omnimarket"
    assert value["branch"] == "jonahgabriel/omn-13226-t2"
    assert value["pr_number"] == 99
    assert value["ticket"] == "OMN-13226"
    assert value["merged_at"] == "2026-06-18T10:00:00Z"
    assert value["event_id"] == event_id


@pytest.mark.unit
def test_publish_pr_merged_event_sasl_config(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer is configured with SASL_SSL, matching the Confluent Cloud pattern."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
        bootstrap_servers="pkc-xxx.us-east-1.aws.confluent.cloud:9092",
        username="apikey",
        password="apisecret",
        repo="OmniNode-ai/omnimarket",
        branch="main",
        pr_number=1,
        ticket="",
        merged_at="2026-06-18T00:00:00Z",
    )

    cfg = _FakeProducer.instances[0].config
    assert cfg["security.protocol"] == "SASL_SSL"
    assert cfg["sasl.mechanisms"] == "PLAIN"
    assert cfg["sasl.username"] == "apikey"
    assert cfg["sasl.password"] == "apisecret"
    assert cfg["bootstrap.servers"] == "pkc-xxx.us-east-1.aws.confluent.cloud:9092"


@pytest.mark.unit
def test_cli_dry_run_no_kafka(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run prints payload and does not instantiate a Producer."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", "jonahgabriel/omn-13226-dry")
    monkeypatch.setenv("PR_NUMBER", "77")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T09:00:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "onex.evt.github.pr-merged.v1" in result.output
    # No Producer should have been instantiated
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_missing_env_exits_nonzero(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI exits non-zero when required env vars are absent."""
    monkeypatch.delenv("PR_REPO", raising=False)
    monkeypatch.delenv("PR_BRANCH", raising=False)
    monkeypatch.delenv("PR_NUMBER", raising=False)
    monkeypatch.delenv("PR_MERGED_AT", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        [],
    )
    assert result.exit_code != 0


@pytest.mark.unit
def test_cli_publishes_when_broker_set(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI calls publish_pr_merged_event when broker env vars are present."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", "jonahgabriel/omn-13226-publish")
    monkeypatch.setenv("PR_NUMBER", "200")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T11:00:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "pkc-xxx:9092")
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "key")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "secret")

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        [],
    )

    assert result.exit_code == 0, result.output
    assert "Published onex.evt.github.pr-merged.v1" in result.output
    assert len(_FakeProducer.instances) == 1
    record = _FakeProducer.instances[0].produced[0]
    assert record["topic"] == "onex.evt.github.pr-merged.v1"
    assert record["value"]["pr_number"] == 200
