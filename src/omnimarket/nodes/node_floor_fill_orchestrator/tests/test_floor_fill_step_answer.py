# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed step answer a scheduled caller reads: free slots, unowned work, the lanes placed and why none was."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedPool,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    answer_json_line,
    main,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    harness,
    overlay,
)

pytestmark = pytest.mark.unit


def test_tick_step_answer_names_the_placed_lanes_and_the_free_slots_read() -> None:
    h = harness(free=20, config=overlay(max_fill_lanes_per_line=1))
    answer = h.orchestrator().run(EnumFloorFillCommand.TICK)
    step = json.loads(answer_json_line(answer))
    started = [r.lane for r in answer.key_results if r.kind == "started"]
    assert started, "the scripted floor places lanes"
    assert step["placed_lanes"] == started
    assert step["free_slots"] == 20
    assert step["status"] == "OK"
    assert step["exit_code"] == 0
    assert step["unread"] == ""
    assert step["why_none"] == ""


def test_inspect_step_answer_reads_the_pool_and_places_nothing() -> None:
    h = harness(free=7)
    answer = h.orchestrator().run(EnumFloorFillCommand.INSPECT)
    step = json.loads(answer_json_line(answer))
    assert h.starter.starts == []
    assert step["free_slots"] == 7
    assert step["unowned_work"] == 4  # the four scripted floor lines are all startable
    assert step["placed_lanes"] == []


def test_a_pool_that_cannot_be_read_is_unread_never_zero_free_slots() -> None:
    h = harness(pool=ScriptedPool(3, unreadable=True))
    answer = h.orchestrator().run(EnumFloorFillCommand.TICK)
    step = json.loads(answer_json_line(answer))
    assert step["free_slots"] is None
    assert step["unread"].startswith("pool:")
    assert step["placed_lanes"] == []
    assert step["why_none"], "an answer that placed nothing says why"


def test_an_answer_with_no_typed_answer_prints_a_failed_line() -> None:
    step = json.loads(
        answer_json_line(
            None, stdout="UNKNOWN fill-state:store-dsn-unset:X\n", exit_code=4
        )
    )
    assert step["status"] == "FAILED"
    assert step["exit_code"] == 4
    assert step["free_slots"] is None
    assert step["unread"] == "fill-state:store-dsn-unset:X"
    assert step["why_none"] == "UNKNOWN fill-state:store-dsn-unset:X"


def test_overlay_from_roots_reads_the_nodes_own_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    section = {**overlay(), "host_id": "host-a"}
    root = tmp_path / "overlays" / "node_floor_fill_orchestrator"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "floor_fill": section,
                "lab_hosts": [{"name": "host-a", "available": True}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    code = main(["--overlay-from-roots", "--answer-json", "inspect"])
    out = capsys.readouterr().out.strip().splitlines()
    step = json.loads(out[-1])
    # The inmemory profile has no floor to read: the refusal is typed, and the host came from the overlay.
    assert code != 0
    assert step["status"] == "DEGRADED"
    assert step["free_slots"] is None
    assert "floor-unread" in step["why_none"]


def test_overlay_from_roots_without_an_overlay_names_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    code = main(["--overlay-from-roots", "--answer-json", "tick"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code != 0
    assert out[0] == "UNKNOWN fill-state:overlay:unreadable"
    assert json.loads(out[-1])["unread"] == "fill-state:overlay:unreadable"


def test_live_profile_with_the_store_dsn_unset_answers_the_refusal_as_unread(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_TEST_UNSET_DSN", raising=False)
    document = {
        "floor_fill": {**overlay(), "host_id": "host-a"},
        "floor_fill_live": {
            "store_dsn_env": "ONEX_TEST_UNSET_DSN",
            "store_schema_name": "public",
            "store_table_name": "floor_fill_state",
            "store_connect_timeout_seconds": 5,
            "floor_fetch_timeout_seconds": 10.0,
            "plan_file_pattern": "{line_id}.md",
            "ledger_path": "/nonexistent/ledger.md",
            "process_output_dir": "/nonexistent/logs",
        },
        "lab_hosts": [{"name": "host-a", "available": True}],
    }
    code = main(
        [
            "--overlay-json",
            json.dumps(document),
            "--this-host",
            "host-a",
            "--profile",
            "live",
            "--answer-json",
            "tick",
        ]
    )
    out = capsys.readouterr().out.strip().splitlines()
    step = json.loads(out[-1])
    assert code == 4
    assert out[0] == "UNKNOWN fill-state:store-dsn-unset:ONEX_TEST_UNSET_DSN"
    assert step["exit_code"] == 4
    assert step["unread"] == "fill-state:store-dsn-unset:ONEX_TEST_UNSET_DSN"
    assert step["free_slots"] is None
    assert step["placed_lanes"] == []
