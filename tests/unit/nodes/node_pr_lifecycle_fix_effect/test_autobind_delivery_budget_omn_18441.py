# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-16156 reason="names the real committed dev-lane broker (omninode-pc.tail75df5e.ts.net) so the refusal message assertion is against the address the publisher actually reports; not a new leak"
"""The occ-autobind publisher waits out a broker recreate (OMN-18441).

The pre-image produced one message and called ``producer.flush(timeout=30)``
exactly once. Its own docstring recorded that librdkafka's per-message
``message.timeout.ms`` defaults to 300000 ms — ten times that flush window — so
the client was still willing to deliver the queued message long after the
publisher had declared it undelivered and exited 1.

That is the whole defect. On 2026-09-16 the ``.201`` dev-lane Redpanda was
recreated at 12:53:26Z and its SCRAM/SASL bootstrap one-shots did not finish
until ~12:58Z; ten runtime rebuilds ran between 05:44Z and 13:32Z. A publish
landing anywhere in one of those windows went red on a message the broker would
have accepted a minute or two later, and a PR whose ONLY push landed there got
no ``Evidence-Source`` stamp and no companion.

What these tests do NOT relax: the OMN-14639 fail-loud contract. A command that
is genuinely undelivered when the declared budget is exhausted still raises.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "publish_occ_autobind_command.py"
)

#: The committed dev-lane broker the publisher reports in its own messages.
_BROKER = "omninode-pc.tail75df5e.ts.net:19092"


def _summary_broker(written: str) -> str:
    """Read the broker back out of the summary line as a FIELD, not a substring.

    The publisher writes the broker inside backticks. Parsing it and comparing
    for equality is both a stronger assertion than a containment check — it
    proves the line names THIS broker and not one that merely contains it — and
    the shape a substring-sanitization analyser has nothing to say about.
    """
    match = re.search(r"`([^`]+)`", written)
    assert match is not None, f"no broker field in the summary line: {written!r}"
    return match.group(1)


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location(
        "publish_occ_autobind_command_omn18441", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecreatingBrokerProducer:
    """A broker that refuses for ``undelivered_flushes`` polls, then accepts.

    This is the live shape of a lane recreate: the connection is refused, the
    message stays queued, and ``_on_delivery`` never fires — until the broker
    comes back, at which point librdkafka delivers the message it has been
    holding all along. ``flush`` returns the remaining queue depth exactly as
    librdkafka does.
    """

    #: passed as ``undelivered_flushes`` to model a broker that never returns.
    NEVER_RECOVERS = -1

    def __init__(
        self, undelivered_flushes: int, config: dict[str, object] | None = None
    ) -> None:
        self._undelivered_flushes = undelivered_flushes
        self.flush_windows: list[float | None] = []
        self.produced: list[dict[str, object]] = []
        self.config: dict[str, object] = dict(config or {})

    def produce(self, **kwargs: object) -> None:
        self.produced.append(kwargs)

    def flush(self, timeout: float | None = None) -> int:
        self.flush_windows.append(timeout)
        if self._undelivered_flushes == self.NEVER_RECOVERS:
            return 1
        if len(self.flush_windows) <= self._undelivered_flushes:
            return 1
        return 0


def _install_recreating_broker(
    monkeypatch: pytest.MonkeyPatch, undelivered_flushes: int
) -> list[_RecreatingBrokerProducer]:
    """Inject a fake ``confluent_kafka`` whose broker recovers mid-wait."""
    built: list[_RecreatingBrokerProducer] = []

    def _factory(config: dict[str, object] | None = None) -> _RecreatingBrokerProducer:
        producer = _RecreatingBrokerProducer(undelivered_flushes, config)
        built.append(producer)
        return producer

    fake_mod = types.ModuleType("confluent_kafka")
    fake_mod.Producer = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_mod)
    return built


def _publish(module: object, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "bootstrap_servers": _BROKER,
        "username": "ci",
        "password": "secret",
        "repo": "OmniNode-ai/omniclaude",
        "pr_number": 2190,
        "ticket": "OMN-18441",
        "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "SCRAM-SHA-256",
        "delivery_budget_seconds": 180.0,
    }
    kwargs.update(overrides)
    return module.publish_occ_autobind_command(**kwargs)  # type: ignore[attr-defined]


@pytest.mark.unit
class TestTheWaitOutlastsARecreate:
    """AC1 / AC2: wait for delivery on a declared budget, producing once."""

    def test_a_broker_that_returns_mid_wait_still_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED against the pre-image: one 30s flush gives up and raises here."""
        module = _load_publisher()
        _install_recreating_broker(monkeypatch, undelivered_flushes=6)

        correlation_id = _publish(module)

        assert correlation_id

    def test_the_message_is_produced_exactly_once_across_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: the wait re-polls the SAME queued message; it never re-produces.

        Re-producing on each attempt would be the obvious retry shape and it is
        the wrong one: a message that was delivered but whose ack was lost would
        then reach the bus twice, and the downstream emitter would be asked to
        mint a companion for the same PR twice.
        """
        module = _load_publisher()
        built = _install_recreating_broker(monkeypatch, undelivered_flushes=6)

        _publish(module)

        assert len(built) == 1, "one producer, not one per attempt"
        assert len(built[0].produced) == 1
        assert len(built[0].flush_windows) > 1, (
            "positive control: this case must actually poll more than once, "
            "otherwise the produce-once assertion above is vacuous"
        )

    def test_a_reachable_broker_is_not_made_to_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the healthy path still returns on the first poll."""
        module = _load_publisher()
        built = _install_recreating_broker(monkeypatch, undelivered_flushes=0)

        _publish(module)

        assert len(built[0].flush_windows) == 1


@pytest.mark.unit
class TestTheBudgetIsDeclared:
    """AC1 / AC3: the budget is an argument, and librdkafka is told about it."""

    def test_the_budget_has_no_default(self) -> None:
        """A defaulted budget here is the hidden value this ticket exists to delete."""
        import inspect

        module = _load_publisher()
        parameter = inspect.signature(
            module.publish_occ_autobind_command  # type: ignore[attr-defined]
        ).parameters["delivery_budget_seconds"]

        assert parameter.default is inspect.Parameter.empty

    def test_the_cli_declares_the_budget_with_an_explicit_default(self) -> None:
        module = _load_publisher()
        options = {
            option.name: option
            for option in module.main.params  # type: ignore[attr-defined]
        }

        assert "delivery_budget_seconds" in options
        assert options["delivery_budget_seconds"].default == pytest.approx(180.0)

    def test_message_timeout_ms_matches_the_declared_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3: librdkafka's own retry window is declared, not left at 300 s.

        The pre-image left ``message.timeout.ms`` unset, so the client's willingness
        to retry (300 s) and the publisher's willingness to wait (30 s) disagreed by
        a factor of ten and neither was written down.
        """
        module = _load_publisher()
        built = _install_recreating_broker(monkeypatch, undelivered_flushes=0)

        _publish(module, delivery_budget_seconds=90.0)

        assert built[0].config["message.timeout.ms"] == 90_000

    def test_a_non_positive_budget_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_publisher()
        _install_recreating_broker(monkeypatch, undelivered_flushes=0)

        with pytest.raises(ValueError, match="delivery_budget_seconds"):
            _publish(module, delivery_budget_seconds=0.0)


@pytest.mark.unit
class TestFailLoudSurvives:
    """AC4: OMN-14639 is unchanged — an undelivered command still raises."""

    def test_budget_exhausted_still_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_publisher()
        built = _install_recreating_broker(
            monkeypatch, undelivered_flushes=_RecreatingBrokerProducer.NEVER_RECOVERS
        )

        with pytest.raises(RuntimeError, match="undelivered"):
            _publish(module, delivery_budget_seconds=1.0)

        assert built[0].flush_windows, (
            "positive control: the budget must be spent flushing, not skipped"
        )

    def test_the_refusal_names_the_broker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_publisher()
        _install_recreating_broker(
            monkeypatch, undelivered_flushes=_RecreatingBrokerProducer.NEVER_RECOVERS
        )

        with pytest.raises(RuntimeError, match=r"omninode-pc\.tail75df5e\.ts\.net"):
            _publish(module, delivery_budget_seconds=1.0)


@pytest.mark.unit
class TestBothOutcomesReachTheJobSummary:
    """AC5: neither a delivery nor a refusal is silent."""

    def test_a_delivery_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = _load_publisher()
        _install_recreating_broker(monkeypatch, undelivered_flushes=2)
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        _publish(module, delivery_budget_seconds=120.0)

        written = summary.read_text(encoding="utf-8")
        assert _summary_broker(written) == _BROKER
        assert "120" in written
        assert "delivered" in written.lower()

    def test_a_refusal_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = _load_publisher()
        _install_recreating_broker(
            monkeypatch, undelivered_flushes=_RecreatingBrokerProducer.NEVER_RECOVERS
        )
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        with pytest.raises(RuntimeError):
            _publish(module, delivery_budget_seconds=1.0)

        written = summary.read_text(encoding="utf-8")
        assert _summary_broker(written) == _BROKER
        assert "undelivered" in written.lower()

    def test_an_unset_summary_path_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the publisher runs outside Actions too."""
        module = _load_publisher()
        _install_recreating_broker(monkeypatch, undelivered_flushes=0)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

        assert _publish(module)
