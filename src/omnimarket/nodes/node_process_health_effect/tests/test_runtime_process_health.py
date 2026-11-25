# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime entry reads the sources, writes the JSON to the contract-declared path and reports unread sources."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError
from omnimarket.nodes.node_process_health_effect.handlers.handler_process_health_run import (
    load_config,
    load_runtime_io,
)
from omnimarket.nodes.node_process_health_effect.runtime_process_health import main

_SOURCE_ENV = (
    "ONEX_PROCESS_HEALTH_OUTPUT",
    "PROCESS_HEALTH_CONFIG_OVERLAY",
    "ONEX_REGISTRY_ROOTS",
    "LANDING_METRICS_HISTORY",
    "PROCESS_HEALTH_LINEAR_SNAPSHOT",
    "THROUGHPUT_TICK_REPO_STATUS_EVENTS",
    "LANDING_STATE_DIR",
)


class RecordedRows:
    """The bus rows reader over a recorded fixture: the lines in a range, narrowed by kind, as the node answers."""

    def __init__(self, text: str) -> None:
        self.lines = tuple(line for line in text.splitlines() if " | " in line)
        self.calls: list[tuple[str, datetime, datetime | None, tuple[str, ...]]] = []

    def __call__(
        self,
        reader: str,
        since: datetime,
        until: datetime | None = None,
        *,
        kinds: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        self.calls.append((reader, since, until, kinds))
        floor = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        return tuple(
            line
            for line in self.lines
            if line.split(" | ")[0] >= floor
            and (not kinds or line.split(" | ")[1] in kinds)
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
    overlay = tmp_path / "process-health-overlay.yaml"
    overlay.write_text("process_health:\n  graph_bucket_hours: 3\n")
    monkeypatch.setenv("PROCESS_HEALTH_CONFIG_OVERLAY", str(overlay))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("ONEX_STATE_DIR", str(state))
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    rows = RecordedRows(ledger.read_text(encoding="utf-8"))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"], rows_reader=rows) == 0
    assert rows.calls[0][0] == load_runtime_io()["ledger"]["reader"]
    assert (
        load_runtime_io()["output"]["default"]
        == "${ONEX_STATE_DIR}/process-health/process-health.json"
    )
    written = json.loads(
        (state / "process-health" / "process-health.json").read_text(encoding="utf-8")
    )
    assert written["range_overlay"]["bucket_hours"] == 3
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


@pytest.mark.unit
def test_range_policy_overlay_uses_the_typed_contract_config(tmp_path: Path) -> None:
    overlay = tmp_path / "range-policy.yaml"
    overlay.write_text(
        "process_health:\n  range_history_days: 90\n  graph_bucket_hours: 3\n"
    )
    config = load_config(overlay_path=overlay)
    assert config.range_history_days == 90
    assert config.graph_bucket_hours == 3
    assert config.reset_at == load_config().reset_at
    overlay.write_text("process_health:\n  graph_bucket_hours: 0\n")
    with pytest.raises(ValueError, match="graph_bucket_hours"):
        load_config(overlay_path=overlay)
    overlay.write_text("process_health:\n  unrecognised_setting: 1\n")
    with pytest.raises(ValueError, match="unrecognised_setting"):
        load_config(overlay_path=overlay)


@pytest.mark.unit
def test_hold_and_release_rows_of_any_age_are_read_beside_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-03-01T10:00:00Z | HOLD | id=h1 | lane=a | an old hold\n"
        "2026-03-02T10:00:00Z | FRICTION | lane=a | actor=x | class=c | cost=~9 lane-minutes | old\n"
        "2026-10-30T10:00:00Z | FRICTION | lane=a | actor=x | class=c | cost=~5 lane-minutes | new\n",
        encoding="utf-8",
    )
    target = tmp_path / "out.json"
    for name in _SOURCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(load_runtime_io()["output"]["env_var"], str(target))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    rows = RecordedRows(ledger.read_text(encoding="utf-8"))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"], rows_reader=rows) == 0
    (_, window_since, _, window_kinds), (_, hold_since, _, hold_kinds) = rows.calls
    assert window_kinds == ()
    assert hold_kinds == ("HOLD", "RELEASE")
    assert hold_since < window_since
    ledger_source = {
        s["name"]: s for s in json.loads(target.read_text(encoding="utf-8"))["sources"]
    }["ledger"]
    assert ledger_source["status"] == "read"


@pytest.mark.unit
def test_a_rows_reader_that_does_not_answer_makes_the_ledger_unread_never_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> tuple[str, ...]:
        raise LedgerRowsUnreadableError("the rows loader exited 69 for reader r")

    target = tmp_path / "out.json"
    for name in _SOURCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(load_runtime_io()["output"]["env_var"], str(target))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--generated-at", "2026-10-30T12:00:00Z"], rows_reader=refuse) == 0
    ledger_source = {
        s["name"]: s for s in json.loads(target.read_text(encoding="utf-8"))["sources"]
    }["ledger"]
    assert ledger_source["status"] == "unread"
    assert "exited 69" in ledger_source["reason"]
