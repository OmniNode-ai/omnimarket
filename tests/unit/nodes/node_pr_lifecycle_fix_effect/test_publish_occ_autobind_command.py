# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher <-> consumer parity for the occ-autobind command (OMN-13990).

The born-path bug was a shape mismatch: the publisher emitted
``{event_id, topic, pr_head_sha, ticket, ...}`` which never validated against
``ModelPrLifecycleFixCommand`` (``extra='forbid'``), so ``payload_type_match``
rejected it and the command was silently DLQ'd — the emitter never fired.

These tests pin the fix end-to-end: the published payload, round-tripped through
JSON (the wire), validates as ``ModelPrLifecycleFixCommand`` with the autobind
block reason AND routes to the OCC autobind adapter through the real handler.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "publish_occ_autobind_command.py"
)


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location(
        "publish_occ_autobind_command", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingAutobindAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        return f"autobound OCC for {repo}#{pr_number}"


@pytest.mark.unit
class TestPublisherConsumerParity:
    def test_block_reason_literal_matches_enum(self) -> None:
        module = _load_publisher()
        block_reason = module._BLOCK_REASON_AUTOBIND  # type: ignore[attr-defined]
        assert block_reason == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND.value

    def test_payload_validates_as_fix_command(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 42, "OMN-1234", str(uuid4())
        )
        # Round-trip through the wire exactly as the runtime consumes it.
        wire = json.loads(json.dumps(payload))
        command = ModelPrLifecycleFixCommand.model_validate(wire)
        assert (
            command.block_reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        )
        assert command.pr_number == 42
        assert command.repo == "OmniNode-ai/omnimarket"
        assert command.ticket_id == "OMN-1234"

    def test_payload_has_no_extra_keys(self) -> None:
        # extra='forbid' means any stray key (the old event_id/topic/pr_head_sha)
        # would raise here — this locks the exact command shape.
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 7, "OMN-1", str(uuid4())
        )
        assert set(payload) == {
            "correlation_id",
            "pr_number",
            "repo",
            "block_reason",
            "ticket_id",
            "requested_at",
        }

    def test_ticketless_payload_uses_none(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 9, "", str(uuid4())
        )
        command = ModelPrLifecycleFixCommand.model_validate(
            json.loads(json.dumps(payload))
        )
        assert command.ticket_id is None

    async def test_published_command_routes_to_autobind_adapter(self) -> None:
        """The full born-path seam: publish -> wire -> validate -> route -> adapter."""
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnibase_infra", 2043, "OMN-9999", str(uuid4())
        )
        command = ModelPrLifecycleFixCommand.model_validate(
            json.loads(json.dumps(payload))
        )

        recording = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFix(occ_autobind_adapter=recording)
        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.error is None
        assert recording.calls == [("OmniNode-ai/omnibase_infra", 2043, "OMN-9999")]


def _required_pr_env() -> dict[str, str]:
    """Minimal env for a real (non-dry-run) invocation, broker vars excluded."""
    return {
        "PR_REPO": "OmniNode-ai/omnimarket",
        "PR_NUMBER": "42",
        "PR_HEAD_SHA": "a" * 40,
        "PR_TITLE": "feat(OMN-1234): example",
    }


@pytest.mark.unit
class TestFailClosedOnTrustedRunner:
    """OMN-14451: the publisher ran green for its entire lifetime while
    publishing nothing, because a missing KAFKA_BOOTSTRAP_SERVERS on the
    trusted self-hosted runner (a real misconfiguration — the runner is a
    container with no ~/.omnibase/.env bind mount) was treated identically to
    a fork PR on ubuntu-latest (an expected, broker-less skip). These tests
    pin the fix: the two cases must now diverge on exit code.

    These tests exercise the ``from-secret`` MODE, so they pin an explicit
    from-secret overlay via ``_override_overlay`` rather than depending on the
    shipped config/ci_bus_lanes.yaml value (which now declares the concrete dev
    broker after OMN-14813 flipped ``dev`` off ``from-secret``). The from-secret
    resolver branch still exists and must stay covered.
    """

    # An explicit from-secret overlay so these tests assert the MODE, decoupled
    # from whatever the shipped dev lane declares (OMN-14813).
    _FROM_SECRET: dict[str, object] = {
        "default": "inmemory",
        "lanes": {"dev": {"broker": "from-secret"}},
    }

    def test_missing_broker_on_trusted_runner_fails_loudly(self) -> None:
        """RED reproduction: broker unset + trusted runner => must NOT be exit 0.

        This is the exact live bug — KAFKA_BOOTSTRAP_SERVERS unresolved on the
        self-hosted omnibase-ci runner — not merely "the var is absent" in the
        abstract; RUNNER_IS_TRUSTED=true pins the "this is the trusted lane"
        condition the old code never checked.
        """
        module = _load_publisher()
        _override_overlay(module, self._FROM_SECRET)
        runner = CliRunner()
        env = {**_required_pr_env(), "RUNNER_IS_TRUSTED": "true"}
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 1, result.output
        assert "TRUSTED" in result.output

    def test_missing_broker_on_fork_runner_skips_gracefully(self) -> None:
        """A fork PR on ubuntu-latest has no broker by design — exit 0 stays."""
        module = _load_publisher()
        _override_overlay(module, self._FROM_SECRET)
        runner = CliRunner()
        env = {**_required_pr_env(), "RUNNER_IS_TRUSTED": "false"}
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output

    def test_missing_runner_is_trusted_flag_fails_fast(self) -> None:
        """A wiring gap (RUNNER_IS_TRUSTED unset) must fail, never default-skip."""
        module = _load_publisher()
        runner = CliRunner()
        env = dict(_required_pr_env())
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        env.pop("RUNNER_IS_TRUSTED", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 1, result.output
        assert "RUNNER_IS_TRUSTED" in result.output

    def test_broker_present_publishes_regardless_of_trust_flag(self) -> None:
        """A resolvable broker takes the normal publish path either way."""
        module = _load_publisher()
        _override_overlay(module, self._FROM_SECRET)
        runner = CliRunner()
        env = {
            **_required_pr_env(),
            "RUNNER_IS_TRUSTED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:0",
        }

        class _FakeDeliveryError(Exception):
            pass

        def _fake_publish(**_kwargs: object) -> str:
            raise _FakeDeliveryError("unreachable in this unit test — expected")

        module.publish_occ_autobind_command = _fake_publish  # type: ignore[attr-defined]
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]

        # Reaches the real publish call (not the broker-missing branch) and
        # fails loudly on delivery error — never silently exits 0.
        assert result.exit_code == 1, result.output
        assert "Delivery error" in result.output


class _FakeProducer:
    """Minimal confluent_kafka.Producer stand-in.

    ``flush()`` returns ``remaining`` — the count of messages still queued when
    the flush window elapses — mirroring librdkafka. On an unreachable broker
    the message stays queued (remaining=1) and the delivery callback never
    fires, which is exactly the false-green condition under test.
    """

    def __init__(self, remaining: int, config: dict[str, object] | None = None) -> None:
        self._remaining = remaining
        self.produced: list[dict[str, object]] = []

    def produce(self, **kwargs: object) -> None:
        self.produced.append(kwargs)

    def flush(self, timeout: float | None = None) -> int:
        # The delivery callback is deliberately NOT invoked: on a connection
        # refusal librdkafka never delivers and never times the message out
        # within the short flush window, so on_delivery stays silent.
        return self._remaining


def _install_fake_confluent_kafka(
    monkeypatch: pytest.MonkeyPatch, remaining: int
) -> None:
    """Inject a fake ``confluent_kafka`` module so the function-local import resolves it."""
    import sys
    import types

    fake_mod = types.ModuleType("confluent_kafka")
    fake_mod.Producer = lambda config=None: _FakeProducer(remaining, config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_mod)


@pytest.mark.unit
class TestFailClosedOnUndeliveredFlush:
    """OMN-14639: reporting success on an UNDELIVERED command is a false green.

    OMN-14451 only closed the *unset broker* case. When the broker is SET but
    unreachable (connection refused), ``producer.flush(timeout=30)`` returns a
    non-zero remaining count while ``_on_delivery`` never fires, so the old code
    ignored the flush result and returned the correlation_id — a green publish
    that delivered nothing. These tests pin the real flush path (the previous
    suite monkeypatched ``publish_occ_autobind_command`` away entirely, so this
    exact branch was never exercised).
    """

    def test_undelivered_message_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RED reproduction: flush leaves 1 message queued => must raise, not return."""
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=1)

        with pytest.raises(RuntimeError, match="undelivered"):
            module.publish_occ_autobind_command(  # type: ignore[attr-defined]
                bootstrap_servers="10.0.0.9:19092",
                username="",
                password="",
                repo="OmniNode-ai/omnimarket",
                pr_number=1774,
                ticket="OMN-14637",
            )

    def test_fully_flushed_message_returns_correlation_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN control: flush drains the queue (remaining=0) => normal success."""
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=0)

        correlation_id = module.publish_occ_autobind_command(  # type: ignore[attr-defined]
            bootstrap_servers="10.0.0.9:19092",
            username="",
            password="",
            repo="OmniNode-ai/omnimarket",
            pr_number=1774,
            ticket="OMN-14637",
        )
        assert isinstance(correlation_id, str)
        assert correlation_id


def _trusted_pr_env(**overrides: str) -> dict[str, str]:
    """A minimal trusted-runner env; broker/overrides supplied per-test."""
    env = {**_required_pr_env(), "RUNNER_IS_TRUSTED": "true"}
    env.update(overrides)
    return env


def _override_overlay(module: object, overlay: dict[str, object]) -> None:
    """Point the publisher at an in-memory lane overlay instead of the file."""

    def _fake(path: object = None) -> dict[str, object]:
        return overlay

    module._load_lane_overlay = _fake  # type: ignore[attr-defined]


class _PublishRecorder:
    """Records the broker the publisher would actually publish to (no I/O)."""

    def __init__(self) -> None:
        self.brokers: list[str] = []

    def __call__(
        self,
        *,
        bootstrap_servers: str,
        username: str,
        password: str,
        repo: str,
        pr_number: int,
        ticket: str,
    ) -> str:
        self.brokers.append(bootstrap_servers)
        return f"cid-{pr_number}"


@pytest.mark.unit
class TestLaneOverlayResolution:
    """OMN-14801: pure lane->broker resolution from the checked-in overlay."""

    def test_missing_overlay_file_returns_empty(self, tmp_path: Path) -> None:
        module = _load_publisher()
        missing = tmp_path / "does_not_exist.yaml"
        assert module._load_lane_overlay(missing) == {}  # type: ignore[attr-defined]

    def test_malformed_overlay_returns_empty(self, tmp_path: Path) -> None:
        module = _load_publisher()
        bad = tmp_path / "ci_bus_lanes.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert module._load_lane_overlay(bad) == {}  # type: ignore[attr-defined]

    def test_overlay_roundtrips_from_disk(self, tmp_path: Path) -> None:
        module = _load_publisher()
        path = tmp_path / "ci_bus_lanes.yaml"
        path.write_text(
            "default: inmemory\nlanes:\n  dev:\n    broker: from-secret\n",
            encoding="utf-8",
        )
        overlay = module._load_lane_overlay(path)  # type: ignore[attr-defined]
        assert module._resolve_lane_broker(overlay, "dev") == (  # type: ignore[attr-defined]
            "from-secret",
            "",
        )

    def test_resolution_modes(self) -> None:
        module = _load_publisher()
        overlay: dict[str, object] = {
            "default": "inmemory",
            "lanes": {
                "dev": {"broker": "from-secret"},
                "stability": {"broker": "inmemory"},
                "prod": {"broker": "broker.example:19092"},
            },
        }
        resolve = module._resolve_lane_broker  # type: ignore[attr-defined]
        assert resolve(overlay, None) == ("no-lane", "")
        assert resolve(overlay, "  ") == ("no-lane", "")
        assert resolve(overlay, "ghost") == ("unknown-lane", "")
        assert resolve(overlay, "dev") == ("from-secret", "")
        assert resolve(overlay, "stability") == ("inmemory", "")
        assert resolve(overlay, "prod") == ("concrete", "broker.example:19092")

    def test_shipped_overlay_declares_concrete_dev_broker(self) -> None:
        """The committed config/ci_bus_lanes.yaml now declares the concrete
        dev-lane broker directly (OMN-14813, completing OMN-14801): the rotation
        settled and the secret dependency was dropped, so ``dev`` resolves to the
        concrete external listener rather than ``from-secret``. This pins that the
        shipped overlay is SECRET-FREE for the dev lane."""
        module = _load_publisher()
        overlay = module._load_lane_overlay()  # type: ignore[attr-defined]
        assert overlay, "shipped config/ci_bus_lanes.yaml should load non-empty"
        assert module._resolve_lane_broker(overlay, "dev") == (  # type: ignore[attr-defined]
            "concrete",
            "omninode-pc.tail75df5e.ts.net:19092",
        )


@pytest.mark.unit
class TestLaneDivergenceGuard:
    """OMN-14801/OMN-14800: a concrete lane broker turns a silently-repointed
    secret into a loud red gate, and prefers the committed broker on a match."""

    _CONCRETE: dict[str, object] = {
        "default": "inmemory",
        "lanes": {"dev": {"broker": "declared:19092"}},
    }

    def test_divergent_secret_on_trusted_fails_loud(self) -> None:
        """The exact incident: injected secret != overlay broker => red, no publish."""
        module = _load_publisher()
        _override_overlay(module, self._CONCRETE)
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="wrong:29092")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "DRIFT" in result.output
        assert "OMN-14800" in result.output
        assert recorder.brokers == []  # never published to the wrong broker

    def test_matching_secret_publishes_to_declared_broker(self) -> None:
        module = _load_publisher()
        _override_overlay(module, self._CONCRETE)
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="declared:19092")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert recorder.brokers == ["declared:19092"]  # overlay-preferred

    def test_secret_unset_on_trusted_publishes_declared(self) -> None:
        module = _load_publisher()
        _override_overlay(module, self._CONCRETE)
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _trusted_pr_env()
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert recorder.brokers == ["declared:19092"]

    def test_shipped_dev_lane_publishes_secret_free(self) -> None:
        """OMN-14813 acceptance: against the REAL shipped config/ci_bus_lanes.yaml
        (no overlay override), a trusted runner with NO KAFKA_BOOTSTRAP_SERVERS
        injected and ``--lane dev`` resolves and publishes to the overlay-declared
        concrete broker. Proves the dev-lane occ-autobind fan-out needs no secret.
        """
        module = _load_publisher()  # uses the shipped overlay (no _override)
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _trusted_pr_env()
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert recorder.brokers == ["omninode-pc.tail75df5e.ts.net:19092"]

    def test_divergent_secret_on_fork_skips(self) -> None:
        module = _load_publisher()
        _override_overlay(module, self._CONCRETE)
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = {
            **_required_pr_env(),
            "RUNNER_IS_TRUSTED": "false",
            "KAFKA_BOOTSTRAP_SERVERS": "wrong:29092",
        }
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output
        assert recorder.brokers == []


@pytest.mark.unit
class TestLaneInMemoryAndNegativeFixtures:
    """OMN-14801 negative fixtures: the in-memory default no-ops; an unknown
    lane or a missing overlay fails loud on the trusted runner (never a silent
    green), degrading to a graceful skip only on fork/cloud runners."""

    def test_inmemory_lane_is_noop_skip(self) -> None:
        module = _load_publisher()
        _override_overlay(
            module,
            {"default": "inmemory", "lanes": {"dev": {"broker": "inmemory"}}},
        )
        recorder = _PublishRecorder()
        module.publish_occ_autobind_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="whatever:19092")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "in-memory" in result.output
        assert recorder.brokers == []  # no cross-process publish

    def test_unknown_lane_on_trusted_fails_loud(self) -> None:
        module = _load_publisher()
        _override_overlay(
            module,
            {"default": "inmemory", "lanes": {"dev": {"broker": "from-secret"}}},
        )
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="x:1")
        result = runner.invoke(module.main, ["--lane", "bogus"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "not declared" in result.output

    def test_missing_overlay_on_trusted_fails_loud(self) -> None:
        """No lane overlay at all -> undeclared lane -> loud fail on trusted."""
        module = _load_publisher()
        _override_overlay(module, {})  # empty == missing file
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="x:1")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "not declared" in result.output

    def test_missing_overlay_on_fork_skips(self) -> None:
        """No lane overlay on a fork/cloud runner -> graceful no-op skip."""
        module = _load_publisher()
        _override_overlay(module, {})
        runner = CliRunner()
        env = {
            **_required_pr_env(),
            "RUNNER_IS_TRUSTED": "false",
            "KAFKA_BOOTSTRAP_SERVERS": "x:1",
        }
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output

    def test_no_lane_on_trusted_fails_loud(self) -> None:
        """Authoring PR on the trusted runner with no --lane is a wiring gap."""
        module = _load_publisher()
        runner = CliRunner()
        env = _trusted_pr_env(KAFKA_BOOTSTRAP_SERVERS="x:1")
        result = runner.invoke(module.main, [], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "--lane was not supplied" in result.output
