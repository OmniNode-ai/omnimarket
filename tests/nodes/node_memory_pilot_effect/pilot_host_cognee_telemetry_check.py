# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pilot-host check: telemetry is off under the pilot environment, with a control that turns it on.

Needs the installed cognee package, so it is not collected by the CI suite (the file name
does not match test_*.py). The pilot host runs it explicitly:
``pytest tests/nodes/node_memory_pilot_effect/pilot_host_cognee_telemetry_check.py``.
Nothing here leaves the process: the sender is stubbed.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_cognee import (
    cognee_environment,
)

from .test_cognee_telemetry_and_environment import _overlay


def test_telemetry_is_off_with_the_pilot_environment_and_on_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    utils = pytest.importorskip("cognee.shared.utils")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        utils._telemetry_sender, "enqueue", lambda payload: sent.append(payload)
    )
    monkeypatch.delenv("ENV", raising=False)
    for key, value in cognee_environment(_overlay(tmp_path)).items():
        monkeypatch.setenv(key, value)
    utils.send_telemetry("pilot probe", uuid.uuid4())
    assert sent == [], "telemetry must be off under the pilot environment"
    monkeypatch.delenv("TELEMETRY_DISABLED")
    utils.send_telemetry("pilot probe", uuid.uuid4())
    assert len(sent) == 1, (
        "control: with the switch removed the same call does queue an event"
    )
