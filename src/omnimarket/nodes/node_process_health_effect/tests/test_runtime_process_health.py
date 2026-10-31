# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime entry reads the sources, writes the JSON to the contract-declared path and reports unread sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_process_health_effect.handlers.handler_process_health_run import (
    load_runtime_io,
)
from omnimarket.nodes.node_process_health_effect.runtime_process_health import main

_SOURCE_ENV = (
    "ONEX_PROCESS_HEALTH_OUTPUT",
    "ONEX_REGISTRY_ROOTS",
    "LANDING_METRICS_HISTORY",
    "PROCESS_HEALTH_LINEAR_SNAPSHOT",
    "THROUGHPUT_TICK_REPO_STATUS_EVENTS",
    "LANDING_STATE_DIR",
)


@pytest.mark.unit
def test_cli_writes_json_to_the_contract_declared_path_and_reports_unread_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger" / "ROLLING_WORK_LEDGER.md"
    ledger.parent.mkdir()
    ledger.write_text(
        "2026-10-30T10:00:00Z | FRICTION | lane=a | actor=claude:sonnet:x | class=gate-conflict | cost=~5 lane-minutes | text\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    for name in _SOURCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("ONEX_STATE_DIR", str(state))
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"]) == 0
    assert (
        load_runtime_io()["output"]["default"]
        == "${ONEX_STATE_DIR}/process-health/process-health.json"
    )
    written = json.loads(
        (state / "process-health" / "process-health.json").read_text(encoding="utf-8")
    )
    sources = {s["name"]: s for s in written["sources"]}
    assert sources["ledger"]["status"] == "read"
    for unread in (
        "linear_snapshot",
        "git_merges",
        "landing_decisions",
        "repo_status_events",
    ):
        assert sources[unread]["status"] == "unread", unread
        assert sources[unread]["reason"]
    metrics = {m["key"]: m for m in written["days"][-1]["window"]["metrics"]}
    assert metrics["friction.lane_stated_minutes"]["value"] == 5.0
    assert metrics["merges.total"]["value"] is None
    assert metrics["merges.total"]["status"] == "unread"


@pytest.mark.unit
def test_cli_output_path_can_be_overridden_by_the_declared_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere" / "out.json"
    for name in _SOURCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(load_runtime_io()["output"]["env_var"], str(target))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(tmp_path / "missing.md"))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1


@pytest.mark.unit
def test_a_clone_root_that_does_not_exist_makes_git_merges_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.json"
    for name in _SOURCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(load_runtime_io()["output"]["env_var"], str(target))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(tmp_path / "missing.md"))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "no-such-home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"]) == 0
    sources = {
        s["name"]: s for s in json.loads(target.read_text(encoding="utf-8"))["sources"]
    }
    assert sources["git_merges"]["status"] == "unread"
    assert "does not exist" in sources["git_merges"]["reason"]
