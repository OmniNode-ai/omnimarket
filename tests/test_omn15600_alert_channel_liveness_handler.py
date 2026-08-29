# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checker runs on the existing tick and says what it found (OMN-15600).

Covers the halves of the ticket that the pure classifier cannot:

* **AC4 — scheduled, not on-demand.**  A webhook that dies at T must be found
  without a human happening to test-fire.  The check rides the heartbeat the
  runtime already emits and self-throttles to a contract-declared interval, so
  there is no new schedule, no polling loop and no ``scripts/**`` entry — the
  three things epic OMN-16776 forbids outright.
* **AC2 — a dead channel fails loudly.**  The verdict leaves the node on its
  own terminal event, which is a surface independent of the Slack channel being
  judged.  It is a classified state, not a log line: a reader can tell DEAD
  from PROBE_ERROR from NOT_CONFIGURED without parsing prose.
* **Thresholds are contract data.**  The probe interval lives in
  ``contract.yaml`` and nowhere else, so changing how often the channel is
  proven alive is a config change, not a deploy of new code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.handler_alert_channel_liveness import (
    HandlerAlertChannelLiveness,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.models import (
    EnumAlertChannelStatus,
    ModelAlertChannelObservation,
    ModelAlertChannelProbeTrigger,
    load_liveness_policy,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODE_DIR = _REPO_ROOT / "src/omnimarket/nodes/node_alert_channel_liveness_effect"
_CONTRACT = _NODE_DIR / "contract.yaml"


class _StubProber:
    """A prober that returns canned observations and counts its calls."""

    def __init__(self, *observations: ModelAlertChannelObservation) -> None:
        self._observations = list(observations)
        self.calls = 0

    def probe(self) -> ModelAlertChannelObservation:
        self.calls += 1
        index = min(self.calls - 1, len(self._observations) - 1)
        return self._observations[index]


class _Clock:
    """A hand-advanced wall clock, so the throttle is tested and not slept on."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _handler(
    prober: _StubProber, clock: _Clock, contract_path: Path | None = None
) -> HandlerAlertChannelLiveness:
    return HandlerAlertChannelLiveness(
        prober=prober,
        clock=clock,
        contract_path=contract_path or _CONTRACT,
    )


def _tick() -> ModelAlertChannelProbeTrigger:
    return ModelAlertChannelProbeTrigger(service_name="omnibase-infra-runtime")


# ---------------------------------------------------------------------------
# AC2 — the verdict is surfaced, not discarded
# ---------------------------------------------------------------------------


def test_a_dead_channel_leaves_the_node_as_a_classified_failure() -> None:
    """DEAD reaches the terminal event with the Slack error that caused it."""
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=False,
            channel_error="channel_not_found",
        )
    )
    result = _handler(prober, _Clock()).handle(_tick())

    assert result.probed is True
    assert result.verdict is not None
    assert result.verdict.status is EnumAlertChannelStatus.DEAD
    assert result.verdict.slack_error == "channel_not_found"
    assert result.healthy is False
    assert result.failure_surfaced is True


def test_a_dead_channel_is_logged_at_error_as_well_as_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bus event is the surface; the ERROR log is the second one.

    Both, because the two fail in different ways: the bus event is lost if the
    publisher is down, and the log is lost if nobody reads it.
    """
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=False,
            auth_error="invalid_auth",
        )
    )
    with caplog.at_level("ERROR"):
        _handler(prober, _Clock()).handle(_tick())
    assert any(
        record.levelname == "ERROR" and "invalid_auth" in record.getMessage()
        for record in caplog.records
    )


def test_a_live_channel_does_not_raise_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The control: a healthy probe surfaces no failure and logs no ERROR."""
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    )
    with caplog.at_level("ERROR"):
        result = _handler(prober, _Clock()).handle(_tick())
    assert result.healthy is True
    assert result.failure_surfaced is False
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_the_probe_never_posts_to_the_channel_it_is_judging() -> None:
    """A liveness check that posts is a liveness check nobody leaves enabled.

    The prober's declared endpoints are read-only Slack Web API methods; the
    node declares no publish topic carrying a Slack command, so it structurally
    cannot send a canary message into the alert channel every interval.
    """
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    publish_topics = contract["event_bus"]["publish_topics"]
    assert not [topic for topic in publish_topics if "slack-publish" in topic]
    endpoints = contract["endpoints"]
    for spec in endpoints.values():
        assert "chat.postMessage" not in spec["url"]


# ---------------------------------------------------------------------------
# AC4 — scheduled, and throttled by contract data
# ---------------------------------------------------------------------------


def test_two_ticks_inside_one_interval_probe_once() -> None:
    """The heartbeat is far more frequent than the channel needs proving."""
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    )
    clock = _Clock(1_000.0)
    handler = _handler(prober, clock)

    first = handler.handle(_tick())
    clock.now += 1.0
    second = handler.handle(_tick())

    assert first.probed is True
    assert second.probed is False
    assert second.verdict is None
    assert prober.calls == 1


def test_a_tick_in_the_next_interval_probes_again() -> None:
    """Throttled is not disabled: the next interval re-proves the channel."""
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    )
    policy = load_liveness_policy(_CONTRACT)
    clock = _Clock(1_000.0)
    handler = _handler(prober, clock)

    handler.handle(_tick())
    clock.now += policy.probe_interval_seconds + 1
    later = handler.handle(_tick())

    assert later.probed is True
    assert prober.calls == 2


def test_raising_the_declared_interval_suppresses_the_same_second_tick(
    tmp_path: Path,
) -> None:
    """Changing the contract changes the behaviour with no code edit."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    original_interval = int(contract["liveness_policy"]["probe_interval_seconds"])

    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    )
    clock = _Clock(1_000.0)
    handler = _handler(prober, clock)
    handler.handle(_tick())
    clock.now += original_interval + 1
    assert handler.handle(_tick()).probed is True

    contract["liveness_policy"]["probe_interval_seconds"] = original_interval * 10
    widened = tmp_path / "contract.yaml"
    widened.write_text(yaml.safe_dump(contract), encoding="utf-8")

    slow_prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    )
    slow_clock = _Clock(1_000.0)
    slow = _handler(slow_prober, slow_clock, contract_path=widened)
    slow.handle(_tick())
    slow_clock.now += original_interval + 1
    assert slow.handle(_tick()).probed is False


def test_a_contract_without_a_liveness_policy_fails_closed(tmp_path: Path) -> None:
    """No Python default stands in for a missing declaration."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    del contract["liveness_policy"]
    broken = tmp_path / "contract.yaml"
    broken.write_text(yaml.safe_dump(contract), encoding="utf-8")

    with pytest.raises(Exception, match="liveness_policy"):
        load_liveness_policy(broken)


def test_no_interval_literal_lives_in_python() -> None:
    """A threshold a code default can satisfy is a threshold nobody can change.

    Mechanically scans every ``.py`` file under the node for a bare integer
    that could act as a fallback probe interval.  Borrowed verbatim in intent
    from OMN-16778's ``test_no_threshold_literal_lives_in_python``.
    """
    offenders: list[str] = []
    pattern = re.compile(
        r"(probe_interval_seconds|interval_seconds)\s*[:=]\s*[0-9]+",
    )
    for path in _NODE_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "probe interval literal found in node source:\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------------------
# The probe error path must reach the same surface as a dead channel
# ---------------------------------------------------------------------------


def test_a_probe_that_blew_up_surfaces_as_a_failure_not_as_silence() -> None:
    """PROBE_ERROR is a reported failure state, not an absent result."""
    prober = _StubProber(
        ModelAlertChannelObservation(
            credentials_present=True,
            transport_error="TimeoutError: probe exceeded 10s",
        )
    )
    result = _handler(prober, _Clock()).handle(_tick())
    assert result.verdict is not None
    assert result.verdict.status is EnumAlertChannelStatus.PROBE_ERROR
    assert result.failure_surfaced is True
    assert result.healthy is False


def test_a_prober_that_raises_is_caught_and_classified_not_propagated() -> None:
    """A raising probe must not DLQ the heartbeat; it must report PROBE_ERROR.

    Raising would route this tick onto a topic named for malformed input and
    lose the one fact worth keeping — that the channel could not be proven.
    """

    class _Exploding:
        calls = 0

        def probe(self) -> ModelAlertChannelObservation:
            self.calls += 1
            raise RuntimeError("dns went away")

    handler = HandlerAlertChannelLiveness(
        prober=_Exploding(),
        clock=_Clock(),
        contract_path=_CONTRACT,
    )
    result = handler.handle(_tick())
    assert result.verdict is not None
    assert result.verdict.status is EnumAlertChannelStatus.PROBE_ERROR
    assert "dns went away" in result.verdict.reason
    assert result.healthy is False
