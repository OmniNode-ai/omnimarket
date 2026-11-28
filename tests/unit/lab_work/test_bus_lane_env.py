# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26725: a ``onex lab-work`` item run by the Done gate reaches the verifier's bus lane.

The refusal "this delegation resolves to the shared kafka bus but names no broker" hit every
contract item that runs ``onex lab-work run`` without ``--bus-lane``. The verifier exports its own
lane to such an item; the CLI reads it through the ``--bus-lane`` option's environment variable.
"""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.lab_work import cli
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

LANE_ENV = "ONEX_LAB_WORK_BUS_LANE"


def _record_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str | None]:
    seen: list[str | None] = []

    @asynccontextmanager
    async def fake_open(**kwargs: object) -> AsyncIterator[object]:
        lane = kwargs.get("lane")
        seen.append(lane if isinstance(lane, str) else None)
        raise LabRunBusError("stop after reading the lane")
        yield None

    monkeypatch.setattr(cli, "open_lab_run_bus", fake_open)
    return seen


def test_cli_reads_the_bus_lane_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LANE_ENV, "dev")
    seen = _record_lane(monkeypatch)
    CliRunner().invoke(cli.lab_work_group, ["hosts", "--bus", "kafka"])
    assert seen == ["dev"]


def test_cli_flag_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LANE_ENV, "dev")
    seen = _record_lane(monkeypatch)
    CliRunner().invoke(
        cli.lab_work_group, ["hosts", "--bus", "kafka", "--bus-lane", "stability"]
    )
    assert seen == ["stability"]


def test_cli_names_no_lane_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LANE_ENV, raising=False)
    seen = _record_lane(monkeypatch)
    CliRunner().invoke(cli.lab_work_group, ["hosts", "--bus", "kafka"])
    assert seen == [None]


def _fake_onex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "onex"
    script.write_text(
        '#!/bin/sh\ntest "$ONEX_LAB_WORK_BUS_LANE" = "$EXPECTED_LANE"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_verifier_exports_its_lane_to_a_lab_work_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fake_onex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("EXPECTED_LANE", "dev")
    monkeypatch.delenv(LANE_ENV, raising=False)
    check = {
        "check_type": "command",
        "check_value": "onex lab-work run --repo OmniNode-ai/omnimarket --kind test -- true",
    }
    ok, message = EvidenceCollector()._run_command_check(check, "OMN-26725")
    assert ok, message


def test_verifier_keeps_a_lane_the_caller_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fake_onex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("EXPECTED_LANE", "stability")
    monkeypatch.setenv(LANE_ENV, "stability")
    check = {
        "check_type": "command",
        "check_value": "onex lab-work run --repo OmniNode-ai/omnimarket --kind test -- true",
    }
    ok, message = EvidenceCollector()._run_command_check(check, "OMN-26725")
    assert ok, message


def test_verifier_leaves_other_commands_without_a_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LANE_ENV, raising=False)
    check = {
        "check_type": "command",
        "check_value": 'test -z "$ONEX_LAB_WORK_BUS_LANE"',
    }
    ok, message = EvidenceCollector()._run_command_check(check, "OMN-26725")
    assert ok, message
