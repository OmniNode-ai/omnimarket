# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of recorded launches: the would-refuse count over the fixture rows, and the printed count line."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_launch_validity_compute.__main__ import main
from omnimarket.nodes.node_launch_validity_compute.handlers.handler_launch_validity_replay import (
    HandlerLaunchValidityReplay,
)
from omnimarket.nodes.node_launch_validity_compute.models import (
    EnumRefusalCode,
    ModelLaunchValidityReplayRequest,
    ModelRecordedLaunch,
)

from .helpers import policy

FIXTURE = Path(__file__).with_name("fixtures") / "recorded_launches.jsonl"
EXPECTED = Path(__file__).with_name("fixtures") / "recorded_launches.expected.json"


def recorded() -> list[ModelRecordedLaunch]:
    return [
        ModelRecordedLaunch.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_launch_validity_replay_counts_the_would_refuse_launches() -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    result = HandlerLaunchValidityReplay().handle(
        ModelLaunchValidityReplayRequest(launches=tuple(recorded()), policy=policy())
    )
    assert result.launches == expected["launches"]
    assert result.would_refuse == expected["would_refuse"]
    assert sorted(result.lanes) == sorted(expected["lanes"])
    assert {c.value: n for c, n in result.by_code.items()} == expected["by_code"]


def test_launch_validity_replay_has_a_positive_control() -> None:
    result = HandlerLaunchValidityReplay().handle(
        ModelLaunchValidityReplayRequest(launches=tuple(recorded()), policy=policy())
    )
    assert result.would_refuse > 0
    assert result.would_refuse < result.launches
    assert (
        EnumRefusalCode.LIVE_OWNER in result.by_code
        or EnumRefusalCode.PR_MERGED in result.by_code
    )


def test_launch_validity_replay_prints_the_count_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(FIXTURE.read_text(encoding="utf-8")))
    assert main([]) == 0
    line = capsys.readouterr().out.strip()
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert line.startswith("LAUNCH-VALIDITY-REPLAY window_days=4 mode=report_only ")
    assert (
        f"launches={expected['launches']} would_refuse={expected['would_refuse']}"
        in line
    )
