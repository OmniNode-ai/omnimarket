# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged effect contract resolves and each operation executes (OMN-20676)."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_merge_sweep_effect.models import (
    ModelMergeSweepLaneRunRequest,
    ModelMergeSweepLoadRequest,
)
from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepCommandOutcome

NAME = "node_merge_sweep_effect"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _resolve(entry: dict[str, Any]) -> tuple[Any, Any]:
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    module, _, name = str(entry["input_model"]).rpartition(".")
    return handler_type, getattr(importlib.import_module(module), name)


class _Runner:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
        self.argvs.append(list(argv))
        if argv[2] == "run":
            return MergeSweepCommandOutcome(
                0, "DETACHED lane=x pid=1 receipt=/r/x.json\n", ""
            )
        return MergeSweepCommandOutcome(0, "", "")


class _Files:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def write_text(self, path: str, text: str) -> None:
        self.written[path] = text

    def read_json(self, path: str) -> dict[str, Any] | None:
        return {
            "exit_code": 0,
            "status": "done",
            "host": "h202",
            "duration_s": 12,
            "result": "ok",
        }


def test_contract_declares_runtime_dispatch_and_no_shared_runtime_bus_block() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert "event_bus" not in contract
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.merge-sweep-effect-requested.v1"
    )
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.merge-sweep-effect-completed.v1",
        "failure": "onex.evt.omnimarket.merge-sweep-effect-failed.v1",
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "load_merge_sweep_facts",
        "run_merge_sweep_lane",
    }


def test_golden_chain_loads_facts_then_runs_a_lane(tmp_path: Path) -> None:
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}

    handler, request_type = _resolve(entries["load_merge_sweep_facts"])
    assert request_type is ModelMergeSweepLoadRequest
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "schema": 1,
                "operator": "op",
                "repos": {"omnimarket": {"default_branch": "dev"}},
                "last_tick": "2026-10-09T12:00:00Z",
                "last_full_resync": "2026-10-09T11:30:00Z",
                "merges": {},
                "prs": {
                    "omnimarket#1": {
                        "facts": {
                            "repo": "omnimarket",
                            "number": 1,
                            "title": "t (OMN-5)",
                            "base": "dev",
                            "head_ref": "h",
                            "head_sha": "a" * 40,
                            "state": "OPEN",
                            "draft": False,
                            "labels": [],
                        },
                        "ci": {
                            "sha": "a" * 40,
                            "verdict": "RED",
                            "total": 1,
                            "runs": [
                                [
                                    "Tests",
                                    "completed",
                                    "failure",
                                    "2026-10-09T11:59:00Z",
                                ]
                            ],
                            "detail": [["Tests", "77", "2026-10-09T11:58:00Z"]],
                        },
                    }
                },
            }
        )
    )
    (tmp_path / "ledger.md").write_text("")
    (tmp_path / "floors.json").write_text(json.dumps({"per_repo": {"omnimarket": 1}}))
    loaded = handler().handle(
        request_type(
            state_path=str(state),
            ledger_path=str(tmp_path / "ledger.md"),
            floors_path=str(tmp_path / "floors.json"),
            now="2026-10-09T12:01:00Z",
            load1=1.0,
            cpus=2,
        )
    )
    assert loaded.ok
    assert loaded.facts is not None
    [pr] = loaded.facts.open_prs
    assert pr.runs is not None
    assert (pr.runs[0].id, pr.runs[0].conclusion) == (77, "failure")
    assert pr.facts_unread == ["files", "ready_at"]
    assert loaded.facts.floors == {"omnimarket": 1.0}
    assert loaded.facts.ticks is None

    handler_type, request_type = _resolve(entries["run_merge_sweep_lane"])
    assert request_type is ModelMergeSweepLaneRunRequest
    runner, lane_files = _Runner(), _Files()
    result = handler_type(runner=runner, files=lane_files).handle(
        request_type(
            runner_script="/s/onex_remote_lane.py",
            brief_path="/tmp/b.md",
            brief_text="brief",
            lane="sweep-fix-1",
            model="sonnet",
            parent="sweep",
            ticket="OMN-1",
            prs=["omnimarket#1"],
            repo="omnimarket",
        )
    )
    assert result.started
    assert (result.exit_code, result.status, result.host) == (0, "done", "h202")
    assert lane_files.written == {"/tmp/b.md": "brief"}
    assert runner.argvs[0][:3] == ["python3", "/s/onex_remote_lane.py", "run"]
    assert runner.argvs[1][2:] == ["wait", "--receipt", "/r/x.json"]
