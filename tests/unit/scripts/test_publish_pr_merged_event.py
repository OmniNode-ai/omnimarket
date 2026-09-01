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
    # The publisher imports its sibling `ci_bus_lanes` module (OMN-17378). At
    # runtime `python scripts/publish_pr_merged_event.py` puts scripts/ on
    # sys.path[0]; a spec_from_file_location load does not, so the harness must
    # reproduce that import context.
    scripts_dir = str(SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
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
# Regression: bus-native publish path must not require omnibase_core (OMN-13379)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_module_imports_without_omnibase_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publisher must import in the minimal CI env (no omnibase_core).

    The pr-merged-publisher workflow installs only confluent-kafka, click, and
    pydantic. Importing the omnimarket.events package (to read the topic
    constant) used to drag in omnibase_core via events/__init__.py and crash the
    job with ModuleNotFoundError. This test simulates that env by making any
    import of omnibase_core OR the omnimarket.events package raise, then loads
    the script fresh and asserts it still resolves the canonical topic.
    """
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "omnibase_core" or name.startswith("omnibase_core."):
            raise ModuleNotFoundError("No module named 'omnibase_core'")
        if name == "omnimarket.events" or name.startswith("omnimarket.events."):
            # The standalone topic loader must not route through the package.
            raise ModuleNotFoundError(
                f"package import '{name}' is forbidden in the minimal publisher env"
            )
        return real_import(name, *args, **kwargs)

    # Drop any cached modules so the fresh load actually re-executes imports.
    for cached in [
        key
        for key in sys.modules
        if key == "publish_pr_merged_event"
        or key == "omnibase_core"
        or key.startswith("omnibase_core.")
        or key == "omnimarket.events"
        or key.startswith("omnimarket.events.")
        or key.startswith("_omnimarket_events_topics_standalone")
    ]:
        monkeypatch.delitem(sys.modules, cached, raising=False)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    module = _load_publisher_module()

    # Topic resolved from topics.py without the package or omnibase_core.
    assert module.TOPIC == "onex.evt.github.pr-merged.v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Stand-in for confluent_kafka.Message.

    Carries broker-assigned coordinates: the publisher records partition/offset
    from the delivery callback as the publish receipt (OMN-17378), and refuses
    to claim success without them.
    """

    def __init__(self, err: object = None, partition: int = 0, offset: int = 0) -> None:
        self._err = err
        self._partition = partition
        self._offset = offset

    def error(self) -> object:
        return self._err

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class _FakeProducer:
    """Minimal confluent_kafka.Producer stand-in with REAL flush() semantics.

    ``flush()`` returns the number of messages still queued when the timeout
    elapses — the return value the pre-OMN-17378 publisher discarded, which is
    what let an undelivered event report success. Class-level knobs let a test
    reproduce the two live failure shapes without a broker:

    ``undelivered``  -> delivery callback never fires and flush() reports the
                        message still queued (unresolvable broker: exactly run
                        33436788824).
    ``silent_drain`` -> flush() drains to 0 but no delivery callback ever ran,
                        so there are no broker-assigned coordinates.
    """

    instances: list[_FakeProducer] = []
    undelivered: bool = False
    silent_drain: bool = False
    delivery_error: object = None
    next_partition: int = 0
    next_offset: int = 0

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.produced: list[dict[str, Any]] = []
        _FakeProducer.instances.append(self)
        self._on_delivery: Any = None
        self._queued = 0

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        on_delivery: Any = None,
    ) -> None:
        self._on_delivery = on_delivery
        self._queued += 1
        self.produced.append(
            {
                "topic": topic,
                "key": key.decode("utf-8"),
                "value": json.loads(value.decode("utf-8")),
            }
        )

    def flush(self, timeout: float = 30.0) -> int:
        if _FakeProducer.undelivered:
            # Broker never resolved: the message stays queued and librdkafka's
            # per-message timeout is far beyond this flush window, so the
            # delivery callback never runs.
            return self._queued
        if _FakeProducer.silent_drain:
            self._queued = 0
            return 0
        if self._on_delivery is not None:
            self._on_delivery(
                _FakeProducer.delivery_error,
                _FakeMessage(
                    partition=_FakeProducer.next_partition,
                    offset=_FakeProducer.next_offset,
                ),
            )
        self._queued = 0
        return 0


@pytest.fixture(autouse=True)
def _reset_producer_instances() -> None:  # type: ignore[return]
    _FakeProducer.instances.clear()
    _FakeProducer.undelivered = False
    _FakeProducer.silent_drain = False
    _FakeProducer.delivery_error = None
    _FakeProducer.next_partition = 0
    _FakeProducer.next_offset = 0


def _override_overlay(module: types.ModuleType, overlay: dict[str, object]) -> None:
    """Point the publisher at an in-memory lane overlay instead of the file."""

    def _fake(path: object = None) -> dict[str, object]:
        return overlay

    module.load_lane_overlay = _fake  # type: ignore[attr-defined]


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

    event_id, partition, offset = publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
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
    # Broker-assigned coordinates are returned, never synthesised.
    assert (partition, offset) == (0, 0)


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
    """CLI publishes when the lane is 'from-secret' and a broker secret is set."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(publisher_module, {"lanes": {"dev": {"broker": "from-secret"}}})

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-publish")
    monkeypatch.setenv("PR_NUMBER", "200")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T11:00:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("RUNNER_IS_TRUSTED", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", BROKER_ENDPOINT)
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "key")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "secret")

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
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
    """The trusted path publishes plaintext to the overlay-declared lane broker.

    This is the live self-hosted-runner shape after OMN-17378: NO
    KAFKA_BOOTSTRAP_SERVERS secret is injected at all, the broker comes from the
    committed lane overlay, and the dev-lane Redpanda has no SASL.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )

    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-lane")
    monkeypatch.setenv("PR_NUMBER", "201")
    monkeypatch.setenv("PR_MERGED_AT", "2026-06-18T11:30:00Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("RUNNER_IS_TRUSTED", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("KAFKA_SASL_USERNAME", raising=False)
    monkeypatch.delenv("KAFKA_SASL_PASSWORD", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
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


# ---------------------------------------------------------------------------
# OMN-17378: the publisher must never report success on an unpublished event
# ---------------------------------------------------------------------------


def _trusted_env(monkeypatch: pytest.MonkeyPatch, pr_number: str = "300") -> None:
    """Minimal trusted-runner env for a merged PR."""
    monkeypatch.setenv("PR_REPO", "OmniNode-ai/omnimarket")
    monkeypatch.setenv("PR_BRANCH", f"{BRANCH_OWNER}/{BRANCH_TICKET.lower()}-guard")
    monkeypatch.setenv("PR_NUMBER", pr_number)
    monkeypatch.setenv("PR_MERGED_AT", "2026-08-31T20:34:39Z")
    monkeypatch.delenv("PR_TICKET", raising=False)
    monkeypatch.setenv("RUNNER_IS_TRUSTED", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("KAFKA_SASL_USERNAME", raising=False)
    monkeypatch.delenv("KAFKA_SASL_PASSWORD", raising=False)


@pytest.mark.unit
def test_publish_raises_when_flush_leaves_message_undelivered(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero flush remainder is a delivery FAILURE, never a success.

    Reproduces run 33436788824 exactly: the broker name does not resolve, the
    message stays queued past the 30s flush window, and librdkafka's per-message
    timeout (300000ms) means the delivery callback never fires. The pre-fix code
    discarded flush()'s return and printed "Published ..." anyway.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _FakeProducer.undelivered = True

    with pytest.raises(RuntimeError) as excinfo:
        publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
            bootstrap_servers=BROKER_ENDPOINT,
            username="",
            password="",
            repo="OmniNode-ai/omnimarket",
            branch=PUBLISH_BRANCH,
            pr_number=2249,
            ticket="OMN-17369",
            merged_at="2026-08-31T20:34:39Z",
        )

    message = str(excinfo.value)
    assert "undelivered" in message
    assert BROKER_ENDPOINT in message


@pytest.mark.unit
def test_publish_raises_when_no_delivery_callback_ran(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drained queue with no delivery callback yields no offset, so no success.

    Without broker-assigned coordinates there is no proof of publication; the
    publisher must refuse rather than synthesise a receipt.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _FakeProducer.silent_drain = True

    with pytest.raises(RuntimeError) as excinfo:
        publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
            bootstrap_servers=BROKER_ENDPOINT,
            username="",
            password="",
            repo="OmniNode-ai/omnimarket",
            branch=PUBLISH_BRANCH,
            pr_number=2250,
            ticket="",
            merged_at="2026-08-31T20:34:39Z",
        )

    assert "no proof of publication" in str(excinfo.value)


@pytest.mark.unit
def test_publish_returns_broker_assigned_partition_and_offset(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful publish returns the coordinates the broker assigned."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _FakeProducer.next_partition = 0
    _FakeProducer.next_offset = 97

    event_id, partition, offset = publisher_module.publish_pr_merged_event(  # type: ignore[attr-defined]
        bootstrap_servers=LOCAL_LANE_ENDPOINT,
        username="",
        password="",
        repo="OmniNode-ai/omnimarket",
        branch=PUBLISH_BRANCH,
        pr_number=2251,
        ticket="OMN-17378",
        merged_at="2026-08-31T21:00:00Z",
    )

    assert event_id
    assert (partition, offset) == (0, 97)


@pytest.mark.unit
def test_cli_fails_loud_when_broker_unresolvable_on_trusted_runner(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEEDED-RED ACCEPTANCE (OMN-17378): trusted + unreachable broker => exit 1.

    The whole defect: eight green runs published nothing. On the trusted path an
    unreachable broker is a defect, so the job must go red.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _FakeProducer.undelivered = True
    _trusted_env(monkeypatch, pr_number="2249")

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 1, result.output
    assert "Delivery error" in result.output
    assert "Published onex.evt.github.pr-merged.v1" not in result.output


@pytest.mark.unit
def test_cli_fails_loud_when_lane_undeclared_on_trusted_runner(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable lane on the trusted runner is a wiring gap, not a no-op."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(publisher_module, {"lanes": {"dev": {"broker": "inmemory"}}})
    _trusted_env(monkeypatch)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "stability"],
    )

    assert result.exit_code == 1, result.output
    assert "is not declared" in result.output
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_fails_loud_when_no_lane_supplied_on_trusted_runner(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --lane on the trusted runner must not silently pick a default bus."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _trusted_env(monkeypatch)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        [],
    )

    assert result.exit_code == 1, result.output
    assert "--lane was not supplied" in result.output
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_fails_loud_on_lane_bus_drift_on_trusted_runner(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected secret diverging from the declared lane broker is red.

    The OMN-14800 silent dev->stability repoint guard, now applied here too. The
    masked secret value is never echoed; only the committed declared broker is.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _trusted_env(monkeypatch)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", BROKER_ENDPOINT)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 1, result.output
    assert "LANE BUS DRIFT" in result.output
    assert BROKER_ENDPOINT not in result.output
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_requires_runner_is_trusted(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNNER_IS_TRUSTED has no default: a wiring gap fails rather than guesses."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _trusted_env(monkeypatch)
    monkeypatch.delenv("RUNNER_IS_TRUSTED", raising=False)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 1, result.output
    assert "RUNNER_IS_TRUSTED" in result.output


@pytest.mark.unit
def test_cli_skips_gracefully_on_untrusted_fork_runner(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful skip survives ONLY on the fork/hosted path, where it is correct.

    A fork PR runs on ubuntu-latest with no broker provisioned by design, so a
    skip there is not a defect and must not red a contributor's merge.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _trusted_env(monkeypatch, pr_number="202")
    monkeypatch.setenv("RUNNER_IS_TRUSTED", "false")

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_skips_when_lane_declares_inmemory(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly in-memory lane is a declared no-op, loud and exit 0."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(publisher_module, {"lanes": {"prod": {"broker": "inmemory"}}})
    _trusted_env(monkeypatch)

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "prod"],
    )

    assert result.exit_code == 0, result.output
    assert "in-memory bus" in result.output
    assert len(_FakeProducer.instances) == 0


@pytest.mark.unit
def test_cli_writes_publish_receipt_to_step_summary(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The run page must answer "did it publish" without a broker probe.

    OMN-17378 fix item 3: topic + event_id + partition + offset land in
    $GITHUB_STEP_SUMMARY on success.
    """
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _FakeProducer.next_offset = 98
    _trusted_env(monkeypatch, pr_number="2250")

    summary = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 0, result.output
    written = summary.read_text(encoding="utf-8")
    assert "pr-merged publish receipt" in written
    assert "onex.evt.github.pr-merged.v1" in written
    assert "`0` / `98`" in written
    assert LOCAL_LANE_ENDPOINT in written


@pytest.mark.unit
def test_no_step_summary_receipt_when_publish_fails(
    publisher_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed publish writes NO receipt — a receipt is proof, not decoration."""
    fake_confluent = types.SimpleNamespace(Producer=_FakeProducer)
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent)
    _override_overlay(
        publisher_module, {"lanes": {"dev": {"broker": LOCAL_LANE_ENDPOINT}}}
    )
    _FakeProducer.undelivered = True
    _trusted_env(monkeypatch, pr_number="2251")

    summary = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    result = CliRunner().invoke(
        publisher_module.main,  # type: ignore[attr-defined]
        ["--lane", "dev"],
    )

    assert result.exit_code == 1, result.output
    assert not summary.exists()


@pytest.mark.unit
def test_committed_overlay_declares_a_concrete_dev_lane_broker(
    publisher_module: types.ModuleType,
) -> None:
    """The real config/ci_bus_lanes.yaml must resolve 'dev' to a concrete broker.

    Guards the routing decision itself: if the overlay ever regresses 'dev' to
    inmemory/from-secret, the trusted publisher would stop publishing, so this
    asserts the committed file, not a fixture.
    """
    overlay = publisher_module.load_lane_overlay()  # type: ignore[attr-defined]
    mode, broker = publisher_module.resolve_lane_broker(overlay, "dev")  # type: ignore[attr-defined]
    assert mode == "concrete"
    assert ":" in broker
