# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab-wide alarm is sent once across hosts, a host-local alarm keeps its host dedup (OMN-24562).

On 2026-11-27 #omninode-notifications got "QUIET HOST h101 ... h202" at 22:14, 22:15 and 22:18 EST:
the alarm runs on four hosts, each dedups only its own ``deliveries.json``, and a host's ledger copy
does not yet hold the row another host just wrote. Quiet-host and throughput-results read the whole
lab, so every host decides the same alarm. The overlay declares one ``lab_wide_sender_host``; the other
hosts decide the alarm and send nothing. Each host here has its own ledger copy and delivery state, as
in production; the alert channel is one shared file, so its lines are the posts the channel received.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import ModelDrainerObservation
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    Watch,
    world,
)

pytestmark = pytest.mark.unit


def _host(
    tmp_path: Path, name: str, channel: Path, **declared: str
) -> ModelSilentFailureAlarmOptions:
    root = tmp_path / name
    root.mkdir()
    options = world(root)
    lib = root / "alert.sh"
    lib.write_text(
        'alert_channel_send() { printf "%s\\n" "$2" >> "$CHANNEL"; echo 1234567890.123; }\n'.replace(
            "$CHANNEL", str(channel)
        )
    )
    return options.model_copy(update={"alert_lib": lib, "host_id": name, **declared})


def _fire(options: ModelSilentFailureAlarmOptions, at) -> tuple[str, ...]:
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in options.host_aliases
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=at)
    )
    assert result.exit_code == 0, result.warnings
    return tuple(result.rows)


def _posts(channel: Path) -> list[str]:
    return channel.read_text().splitlines() if channel.exists() else []


def test_lab_wide_alarm_two_hosts_in_one_window_send_one_message(
    tmp_path: Path,
) -> None:
    channel = tmp_path / "channel.txt"
    declared = {"lab_wide_sender_host": "h053"}
    sender = _host(tmp_path, "h053", channel, **declared)
    other = _host(tmp_path, "h051", channel, **declared)
    _fire(sender, HOST_NOW)
    _fire(other, HOST_NOW + timedelta(minutes=1))
    quiet = [line for line in _posts(channel) if "QUIET HOST" in line.upper()]
    assert len(quiet) == 1, quiet


def test_lab_wide_alarm_declared_sender_still_sends_when_other_host_ticks_first(
    tmp_path: Path,
) -> None:
    channel = tmp_path / "channel.txt"
    declared = {"lab_wide_sender_host": "h053"}
    sender = _host(tmp_path, "h053", channel, **declared)
    other = _host(tmp_path, "h051", channel, **declared)
    _fire(other, HOST_NOW)
    assert _posts(channel) == []
    _fire(sender, HOST_NOW + timedelta(minutes=1))
    assert len([p for p in _posts(channel) if "QUIET HOST" in p.upper()]) == 1


def test_lab_wide_alarm_control_without_a_declared_sender_every_host_sends(
    tmp_path: Path,
) -> None:
    channel = tmp_path / "channel.txt"
    first = _host(tmp_path, "h053", channel)
    second = _host(tmp_path, "h051", channel)
    _fire(first, HOST_NOW)
    _fire(second, HOST_NOW + timedelta(minutes=1))
    assert len([p for p in _posts(channel) if "QUIET HOST" in p.upper()]) == 2


def test_host_local_alarm_keeps_its_host_dedup_on_a_non_sender_host(
    tmp_path: Path,
) -> None:
    """Tick-liveness reads this host's own controller: a non-sender host still sends it."""
    channel = tmp_path / "channel.txt"
    other = _host(tmp_path, "h051", channel, lab_wide_sender_host="h053")
    rows = _fire(other, HOST_NOW + timedelta(hours=3))
    assert any("class=tick-liveness" in row for row in rows), rows
    assert any("tick" in p.lower() for p in _posts(channel)), _posts(channel)
    assert not [p for p in _posts(channel) if "QUIET HOST" in p.upper()]


def test_installer_document_carries_host_id_and_sender_and_loads_as_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit's declared document names this host and the sender, and loads as the alarm's options."""
    import json

    from tests.nodes.node_host_installer_registry_units.test_alarm_dispatch_command import (
        UNIT,
        _handler,
        _request,
    )

    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h051")
    monkeypatch.setenv("ONEX_ALARM_LAB_WIDE_SENDER_HOST", "h053")
    rendered = _handler().handle(_request(tmp_path))
    assert rendered.exit_code == 0, rendered.stderr
    document = json.loads(rendered.config)
    assert (document["host_id"], document["lab_wide_sender_host"]) == ("h051", "h053")
    assert UNIT == "silent-failure-alarm"
    options = ModelSilentFailureAlarmOptions.model_validate(
        {k: v for k, v in document.items() if k != "host_table"}
    )
    assert options.lab_wide_sender_host == "h053"
