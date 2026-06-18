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
BRANCH_OWNER = "contributor"
BRANCH_TICKET = "OMN-13226"
LEGACY_BRANCH_TICKET = "OMN-99"
EXAMPLE_FEATURE_BRANCH = f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-some-feature"
PUBLISH_BRANCH = f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-t2"
BROKER_ENDPOINT = "broker.example.invalid:9092"
LOCAL_LANE_ENDPOINT = (
    "broker.local.invalid:19092"  # local lane plaintext broker stand-in
)


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
        branch=EXAMPLE_FEATURE_BRANCH,
        pr_number=42,
        ticket="OMN-12345",
        merged_at="2026-06-18T12:00:00Z",
        event_id="test-uuid",
    )

    assert payload["topic"] == "onex.evt.github.pr-merged.v1"
    assert payload["repo"] == "OmniNode-ai/omnimarket"
    assert payload["branch"] == EXAMPLE_FEATURE_BRANCH
    assert payload["pr_number"] == 42
    assert payload["ticket"] == "OMN-12345"
    assert payload["merged_at"] == "2026-06-18T12:00:00Z"
    assert payload["event_id"] == "test-uuid"
    assert "published_at" in payload


@pytest.mark.unit
def test_extract_ticket_from_branch(publisher_module: types.ModuleType) -> None:
    """_extract_ticket pulls OMN-XXXX from a branch name."""
    fn = publisher_module._extract_ticket  # type: ignore[attr-defined]
    assert fn(f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-t2-publisher") == BRANCH_TICKET
    assert (
        fn(f"{BRANCH_OWNER}/{LEGACY_BRANCH_TICKET.lower()}-fix") == LEGACY_BRANCH_TICKET
    )
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
        branch=PUBLISH_BRANCH,
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
    assert value["branch"] == PUBLISH_BRANCH
    assert value["pr_number"] == 99
    assert value["ticket"] == "OMN-13226"
    assert value["merged_at"] == "2026-06-18T10:00:00Z"
    assert value["event_id"] == event_id


@pytest.mark.unit
def test_publish_pr_merged_event_sasl_config_when_creds_present(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SASL_SSL transport is used when SASL credentials are supplied (cloud broker)."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
        bootstrap_servers=BROKER_ENDPOINT,
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
    assert cfg["bootstrap.servers"] == BROKER_ENDPOINT


@pytest.mark.unit
def test_publish_pr_merged_event_plaintext_when_no_creds(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plaintext transport (no SASL keys) is used for the local lane broker.

    The local lane Redpanda broker has no SASL; the producer must connect in
    plaintext, resolving the endpoint purely from KAFKA_BOOTSTRAP_SERVERS.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
        bootstrap_servers=LOCAL_LANE_ENDPOINT,
        username="",
        password="",
        repo="OmniNode-ai/omnimarket",
        branch="main",
        pr_number=2,
        ticket="",
        merged_at="2026-06-18T00:00:00Z",
    )

    cfg = _FakeProducer.instances[0].config
    assert cfg["bootstrap.servers"] == LOCAL_LANE_ENDPOINT
    # No SASL/SSL keys when credentials are absent (plaintext LAN broker).
    assert "security.protocol" not in cfg
    assert "sasl.mechanisms" not in cfg
    assert "sasl.username" not in cfg
    assert "sasl.password" not in cfg


@pytest.mark.unit
def test_cli_dry_run_no_kafka(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run prints payload and does not instantiate a Producer."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-dry")
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
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-publish")
    monkeypatch.setenv("PR_NUMBER", "200")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T11:00:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", BROKER_ENDPOINT)
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


@pytest.mark.unit
def test_cli_publishes_local_lane_plaintext_no_sasl(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI publishes via plaintext when a broker is set but no SASL creds.

    This is the self-hosted-runner path: KAFKA_BOOTSTRAP_SERVERS resolves to the
    local lane broker (from ~/.omnibase/.env) with no SASL credentials, so the
    event still publishes to the canonical topic over plaintext.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-lane")
    monkeypatch.setenv("PR_NUMBER", "201")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T11:30:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", LOCAL_LANE_ENDPOINT)
    monkeypatch.delenv("KAFKA_SASL_USERNAME", raising=False)
    monkeypatch.delenv("KAFKA_SASL_PASSWORD", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        [],
    )

    assert result.exit_code == 0, result.output
    assert "Published onex.evt.github.pr-merged.v1" in result.output
    assert len(_FakeProducer.instances) == 1
    cfg = _FakeProducer.instances[0].config
    assert cfg["bootstrap.servers"] == LOCAL_LANE_ENDPOINT
    assert "security.protocol" not in cfg
    record = _FakeProducer.instances[0].produced[0]
    assert record["topic"] == "onex.evt.github.pr-merged.v1"
    assert record["value"]["pr_number"] == 201


@pytest.mark.unit
def test_cli_skips_gracefully_when_broker_unset(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI exits 0 with a loud warning when KAFKA_BOOTSTRAP_SERVERS is unset.

    A broker misconfiguration (e.g. misrouted onto a cloud runner with no broker
    provisioned) must be visible in the logs but must NOT red every merge: the
    publisher skips gracefully (exit 0) and instantiates no Producer.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-skip")
    monkeypatch.setenv("PR_NUMBER", "202")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T12:00:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("KAFKA_SASL_USERNAME", raising=False)
    monkeypatch.delenv("KAFKA_SASL_PASSWORD", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        [],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "KAFKA_BOOTSTRAP_SERVERS is not set" in result.output
    # No Producer should have been instantiated when skipping.
    assert len(_FakeProducer.instances) == 0
