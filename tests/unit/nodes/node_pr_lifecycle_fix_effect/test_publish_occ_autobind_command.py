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
    """

    def test_missing_broker_on_trusted_runner_fails_loudly(self) -> None:
        """RED reproduction: broker unset + trusted runner => must NOT be exit 0.

        This is the exact live bug — KAFKA_BOOTSTRAP_SERVERS unresolved on the
        self-hosted omnibase-ci runner — not merely "the var is absent" in the
        abstract; RUNNER_IS_TRUSTED=true pins the "this is the trusted lane"
        condition the old code never checked.
        """
        module = _load_publisher()
        runner = CliRunner()
        env = {**_required_pr_env(), "RUNNER_IS_TRUSTED": "true"}
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, [], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 1, result.output
        assert "TRUSTED" in result.output

    def test_missing_broker_on_fork_runner_skips_gracefully(self) -> None:
        """A fork PR on ubuntu-latest has no broker by design — exit 0 stays."""
        module = _load_publisher()
        runner = CliRunner()
        env = {**_required_pr_env(), "RUNNER_IS_TRUSTED": "false"}
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, [], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output

    def test_missing_runner_is_trusted_flag_fails_fast(self) -> None:
        """A wiring gap (RUNNER_IS_TRUSTED unset) must fail, never default-skip."""
        module = _load_publisher()
        runner = CliRunner()
        env = dict(_required_pr_env())
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        env.pop("RUNNER_IS_TRUSTED", None)
        result = runner.invoke(module.main, [], env=env)  # type: ignore[attr-defined]

        assert result.exit_code == 1, result.output
        assert "RUNNER_IS_TRUSTED" in result.output

    def test_broker_present_publishes_regardless_of_trust_flag(self) -> None:
        """A resolvable broker takes the normal publish path either way."""
        module = _load_publisher()
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
        result = runner.invoke(module.main, [], env=env)  # type: ignore[attr-defined]

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
