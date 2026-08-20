# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher <-> consumer seam for the occ-companion-effect command (OMN-14941).

The occ-autobind born-path bug (OMN-13990) was a publisher payload that never
validated against its consumer model, so every command was silently DLQ'd and
the effect never fired. These tests pin the NEW publisher's seam the hard way:
the ACTUAL ``--dry-run`` CLI output (the exact JSON the workflow would put on
the wire) is fed to ``ModelOccCompanionEffectRequest.model_validate`` in the
SAME test, asserting:

* ``mode == "mutate"`` — the model defaults to ``dry_run`` (fail-safe), so an
  omitted mode is a silent never-mint (the optional-input-silent-skip trap);
* ``pr_number`` is an ``int`` (GHA env is a string; the publisher casts);
* runner/verifier take the model defaults and differ (OMN-12791);
* every legacy occ-autobind field is REJECTED (``extra='forbid'``).

Plus: publisher-side idempotency (an already-bound PR body skips the publish
loudly), the Kafka key/topic shape, and the OMN-14639 fail-loud flush.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)

_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "publish_occ_companion_effect_command.py"
)
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_occ_companion_effect"
    / "contract.yaml"
)

_LEGACY_AUTOBIND_FIELDS = (
    "block_reason",
    "ticket_id",
    "requested_at",
    "pr_head_sha",
    "event_id",
    "topic",
)


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location(
        "publish_occ_companion_effect_command", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_pr_env(**overrides: str) -> dict[str, str]:
    env = {
        "PR_REPO": "OmniNode-ai/omnimarket",
        "PR_NUMBER": "42",
        "PR_HEAD_SHA": "a" * 40,
        "PR_BODY": "Closes OMN-14941",
    }
    env.update(overrides)
    return env


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
    ) -> str:
        self.brokers.append(bootstrap_servers)
        return f"cid-{pr_number}"


@pytest.mark.unit
class TestCrossBoundarySeam:
    """The mandated seam test: real publisher dry-run output -> real model."""

    def test_dry_run_output_validates_as_effect_request(self) -> None:
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(),
        )
        assert result.exit_code == 0, result.output

        # The emitted JSON payload is the last block of the dry-run output —
        # parse the ACTUAL wire bytes, not a re-built dict.
        payload = json.loads(result.output[result.output.index("{") :])
        command = ModelOccCompanionEffectRequest.model_validate(payload)

        # The optional-input-silent-skip trap: mode MUST be explicit "mutate";
        # a model-default dry_run command would read+compute and never mint.
        assert command.mode == "mutate"
        # GHA env is a string; the wire value must already be an int.
        assert isinstance(payload["pr_number"], int)
        assert command.pr_number == 42
        assert command.repo == "OmniNode-ai/omnimarket"
        # correlation_id is a str uuid4 on the wire; the model coerces to UUID.
        assert isinstance(command.correlation_id, UUID)
        # occ_repo/runner/verifier are omitted -> model defaults apply, and
        # runner != verifier (OMN-12791).
        assert command.runner == "node_occ_companion_compute"
        assert command.verifier == "occ-evidence-source-autobind"
        assert command.runner != command.verifier

    def test_injected_legacy_autobind_fields_are_rejected(self) -> None:
        """extra='forbid' seam: each legacy occ-autobind field poisons the
        payload — the exact wrong-shape-silently-DLQd class (OMN-13990)."""
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(),
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{") :])

        for legacy in _LEGACY_AUTOBIND_FIELDS:
            with pytest.raises(ValidationError):
                ModelOccCompanionEffectRequest.model_validate(
                    {**payload, legacy: "poison"}
                )

    def test_payload_has_exactly_the_command_fields(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 7, "00000000-0000-4000-8000-000000000000"
        )
        assert set(payload) == {"repo", "pr_number", "mode", "correlation_id"}
        assert payload["mode"] == "mutate"

    def test_topic_matches_the_contract_declared_command_topic(self) -> None:
        """The publisher's topic constant must be the exact topic the node's
        contract subscribes to — a mismatch is a publish into the void."""
        module = _load_publisher()
        contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        topic = module.TOPIC  # type: ignore[attr-defined]
        assert topic == "onex.cmd.omnimarket.occ-companion-effect-requested.v1"
        assert topic == contract["runtime_dispatch"]["command_topic"]
        assert topic in contract["event_bus"]["subscribe_topics"]

    def test_non_integer_pr_number_fails_fast(self) -> None:
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(PR_NUMBER="not-a-number"),
        )
        assert result.exit_code == 1, result.output
        assert "PR_NUMBER must be an integer" in result.output


@pytest.mark.unit
class TestPublisherSideIdempotency:
    """OMN-14941: an already-bound product PR skips the publish loudly."""

    def test_already_bound_body_skips_and_never_publishes(self) -> None:
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Closes OMN-14941\n\nEvidence-Source: OCC#4242",
            RUNNER_IS_TRUSTED="true",
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert "already" in result.output
        assert recorder.brokers == []

    def test_already_bound_body_skips_even_in_dry_run(self) -> None:
        """The skip is a semantic gate, not a transport branch — dry-run output
        must not advertise a payload that the live path would refuse to send."""
        module = _load_publisher()
        runner = CliRunner()
        env = _required_pr_env(PR_BODY="Evidence-Source: OCC#9")
        result = runner.invoke(module.main, ["--dry-run"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert "{" not in result.output  # no payload emitted

    def test_unbound_body_publishes_secret_free_on_shipped_dev_lane(self) -> None:
        """E2 acceptance shape: against the REAL shipped config/ci_bus_lanes.yaml,
        a trusted runner with NO KAFKA_BOOTSTRAP_SERVERS injected and --lane dev
        resolves and publishes to the committed concrete broker (OMN-14813 —
        the born path needs no secret)."""
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(RUNNER_IS_TRUSTED="true")
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert (
            recorder.brokers
            == [
                "omninode-pc.tail75df5e.ts.net:19092"  # onex-allow-test-fixture OMN-16156 reason="asserts the real committed dev-lane broker resolves correctly from config"
            ]
        )

    def test_missing_runner_is_trusted_flag_fails_fast(self) -> None:
        """The RUNNER_IS_TRUSTED wiring-gap fail-fast carries over (OMN-14451)."""
        module = _load_publisher()
        runner = CliRunner()
        env = _required_pr_env()
        env.pop("RUNNER_IS_TRUSTED", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "RUNNER_IS_TRUSTED" in result.output


class _FakeProducer:
    """Minimal confluent_kafka.Producer stand-in (mirrors the autobind tests):
    ``flush()`` returns the count of messages still queued when the window
    elapses; the delivery callback deliberately never fires."""

    instances: list[_FakeProducer] = []

    def __init__(self, remaining: int, config: dict[str, object] | None = None) -> None:
        self._remaining = remaining
        self.produced: list[dict[str, object]] = []
        _FakeProducer.instances.append(self)

    def produce(self, **kwargs: object) -> None:
        self.produced.append(kwargs)

    def flush(self, timeout: float | None = None) -> int:
        return self._remaining


def _install_fake_confluent_kafka(
    monkeypatch: pytest.MonkeyPatch, remaining: int
) -> None:
    _FakeProducer.instances = []
    fake_mod = types.ModuleType("confluent_kafka")
    fake_mod.Producer = lambda config=None: _FakeProducer(remaining, config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_mod)


@pytest.mark.unit
class TestKafkaWireShape:
    """Key/topic/value shape + the OMN-14639 fail-loud flush."""

    def test_produce_uses_companion_effect_key_and_canonical_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=0)

        correlation_id = module.publish_occ_companion_effect_command(  # type: ignore[attr-defined]
            bootstrap_servers="10.0.0.9:19092",
            username="",
            password="",
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
        )
        (producer,) = _FakeProducer.instances
        (produced,) = producer.produced
        assert produced["topic"] == module.TOPIC  # type: ignore[attr-defined]
        assert produced["key"] == b"occ-companion-effect/OmniNode-ai/omnimarket/42"
        wire = json.loads(bytes(produced["value"]).decode("utf-8"))  # type: ignore[arg-type]
        command = ModelOccCompanionEffectRequest.model_validate(wire)
        assert command.mode == "mutate"
        assert str(command.correlation_id) == correlation_id

    def test_undelivered_message_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OMN-14639: flush leaves 1 message queued => must raise, not return."""
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=1)

        with pytest.raises(RuntimeError, match="undelivered"):
            module.publish_occ_companion_effect_command(  # type: ignore[attr-defined]
                bootstrap_servers="10.0.0.9:19092",
                username="",
                password="",
                repo="OmniNode-ai/omnimarket",
                pr_number=42,
            )
