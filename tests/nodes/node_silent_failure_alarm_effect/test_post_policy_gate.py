# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The silent-failure alarm's send goes through the channel post policy (OMN-24562)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models.model_silent_failure_alarm_options import (
    ModelSilentFailureAlarmOptions,
)

pytestmark = pytest.mark.unit

_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect/overlay.example.yaml"
)


def _handler(tmp_path: Path) -> tuple[HandlerSilentFailureAlarm, Path]:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))["post_policy"]
        )
    )
    sent = tmp_path / "sent.txt"
    lib = tmp_path / "alert.sh"
    lib.write_text(
        f'alert_channel_send() {{ echo "$2" >> {sent}; echo 1234567890.123; }}\n'
    )
    options = ModelSilentFailureAlarmOptions(
        registry_root=tmp_path,
        alert_lib=lib,
        alert_env_file=tmp_path / "empty.env",
        post_policy_file=policy,
        post_policy_state_file=tmp_path / "gate.json",
    )
    return HandlerSilentFailureAlarm(options), sent


def test_quiet_host_sent_by_four_ticks_reaches_the_channel_once(tmp_path: Path) -> None:
    handler, sent = _handler(tmp_path)
    results = [
        handler._deliver("quiet_host_alarm", "QUIET HOST: lab-wide capture silent")
        for _ in range(4)
    ]
    assert all(delivered for delivered, _, _ in results)
    assert len(sent.read_text().splitlines()) == 1


def test_self_healing_drift_never_reaches_the_channel(tmp_path: Path) -> None:
    handler, sent = _handler(tmp_path)
    delivered, detail, _ = handler._deliver(
        "host_drift", "[host-drift] DETECTED h052 plugin behind, syncing now"
    )
    assert delivered
    assert "self-healing" in detail
    assert not sent.exists()
