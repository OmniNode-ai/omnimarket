# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestration-alignment line reads what the ledger and the watcher prove, not what a lane's text mentions.

Four rules, one fixture each, every fixture run under two profiles (in memory, and through the effect entry with a
deployment overlay, ``ONEX_FACTORY_FLOOR_OVERLAY``):

- merged work outranks a stale blocked or partial lane TERMINAL;
- the task-to-ticket map STATUS row puts its ticket and pull request on the task's row and station;
- a TERMINAL that only names a task id in its text is not that task's evidence;
- a built task behind a gate that reads fail shows "built ahead of gate" and is left out of the built percent.

The label, the binding cells, the map row's state and the gate verdicts come from the compute contract.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorMerge,
    ModelFloorSelection,
    ModelFloorWatcher,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.runtime_factory_floor import main
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_contract,
    load_plans,
    parse_ledger_text,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
EFFECT_CONTRACT = NODES / "node_factory_floor_effect/contract.yaml"
COMPUTE_CONTRACT = NODES / "node_factory_floor_compute/contract.yaml"
FIXTURE_KB = Path(__file__).resolve().parent / "fixtures/floor_truth"
LINE = "2026-10-29-orchestration-alignment-plan"
NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=UTC)

# The watcher's merge list: W0.1's mapped pull request and W1.1's, each titled with its ticket.
MERGES = (
    (
        "knowledge_base_internal#1573",
        "feat(OMN-33001): capture the workflow identities",
    ),
    ("omnimarket#9101", "feat(OMN-33003): report workflow-level results"),
)

# The ledger, in the row form the ledger reader prints.
LEDGER = "\n".join(
    (
        # A stale TERMINAL of another lane, bound to W0.1 by its task cell, ended blocked days before the merge.
        "L1 2026-10-31T08:19:23Z | TERMINAL | lane=retro-track-b | task=W0.1 | outcome=blocked | W0.1 capture blocked",
        "L2 2026-10-31T08:20:00Z | TERMINAL | lane=retro-track-c | task=W0.2 | outcome=partial | W0.2 partial",
        # The task-to-ticket map: one STATUS row per task, scoped to the plan by its subject cell.
        f"L3 2026-11-02T14:00:00Z | STATUS | lane=fill-orchestration | state=task-map | subject={LINE} | task=W0.1 | ticket=OMN-33001 | pr=OmniNode-ai/knowledge_base_internal#1573 | map",
        # W0.2's pull request is in a repository the watcher does not read: the map records that it merged.
        f"L4 2026-11-02T14:00:01Z | STATUS | lane=fill-orchestration | state=task-map | subject={LINE} | task=W0.2 | ticket=OMN-33002 | pr=OmniNode-ai/knowledge_base_internal#1583 | outcome=merged | map",
        # A ticket-minting lane that only names W1.2a in its free text, carrying the plan's epic ticket.
        "L5 2026-11-02T14:10:00Z | TERMINAL | lane=tickets-orchestration | ticket=OMN-33000 | outcome=done | minted the tickets for W1.2a and W1.2b",
        # Gate G1b reads fail.
        "L6 2026-11-02T14:20:00Z | STATUS | lane=wave0-unblock | state=gate | subject=G1b | verdict=fail | gate G1b failed",
    )
)


def _compute_config() -> ModelFactoryFloorConfig:
    raw = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))
    return ModelFactoryFloorConfig.model_validate(raw["config"])


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURE_KB, kb)
    return kb


def _in_memory(tmp_path: Path) -> dict[str, Any]:
    """The plan read with the effect contract's values, the ledger parsed by its reader, the handler called in process."""
    runtime = load_contract(EFFECT_CONTRACT).runtime
    request = ModelFactoryFloorRequest(
        generated_at=NOW,
        config=_compute_config(),
        selection=ModelFloorSelection(plan_path_contains=(LINE,)),
        plans=load_plans(
            kb=_kb(tmp_path),
            plans_relpath=runtime.plans_relpath,
            manifest_suffix=runtime.manifest_suffix,
            header_keys=runtime.plan_header_keys,
            heading_task_patterns=runtime.heading_task_patterns,
            contains=(LINE,),
            plans_today=None,
        ),
        watcher=ModelFloorWatcher(
            last_tick=NOW,
            merges=tuple(
                ModelFloorMerge(key=key, merged_at=NOW, title=title)
                for key, title in MERGES
            ),
        ),
        ledger=parse_ledger_text(LEDGER),
    )
    return HandlerFactoryFloor().handle(request).model_dump(mode="json")


def _overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The effect entry with a deployment overlay naming the line, writing the JSON the floor publishes."""
    runtime = load_contract(EFFECT_CONTRACT).runtime
    home = tmp_path / "home"
    watcher = home / runtime.sources.watcher_relpath
    watcher.parent.mkdir(parents=True)
    watcher.write_text(
        json.dumps(
            {
                "last_tick": "2026-11-02T14:59:00Z",
                "prs": {},
                "merges": {
                    key: {
                        "merged_at": "2026-11-02T14:50:00Z",
                        "title": title,
                        "head_ref": "",
                    }
                    for key, title in MERGES
                },
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"plan_path_contains": [LINE]}), encoding="utf-8")
    monkeypatch.setenv(runtime.overlay_file_env, str(overlay))
    ledger = tmp_path / "ledger.txt"
    ledger.write_text(LEDGER + "\n", encoding="utf-8")
    argv = [
        *("--home", str(home)),
        *("--kb", str(_kb(tmp_path))),
        *("--now", "2026-11-02T15:00:00Z"),
        *("--ledger-rows-file", str(ledger)),
    ]
    assert main(argv) == 0
    document: dict[str, Any] = json.loads(
        (home / runtime.output.relpath).read_text(encoding="utf-8")
    )
    return document


PROFILES: dict[str, Callable[[Path, pytest.MonkeyPatch], dict[str, Any]]] = {
    "in-memory": lambda tmp_path, _monkeypatch: _in_memory(tmp_path),
    "overlay": _overlay,
}


def _line(document: dict[str, Any]) -> dict[str, Any]:
    (line,) = (line for line in document["lines"] if line["line_id"] == LINE)
    assert line["source"] == "manifest"
    return line


def _stations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {station["id"]: station for station in _line(document)["stations"]}


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_stale_terminal_merged_task_reads_merged(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged task reads merged although a stale blocked (W0.1) or partial (W0.2) TERMINAL is bound to it."""
    stations = _stations(PROFILES[profile](tmp_path, monkeypatch))
    w01, w02 = stations["W0.1"], stations["W0.2"]
    assert (w01["state"], w01["items"][0]["note"]) == ("done", "PRs merged")
    assert (w02["state"], w02["items"][0]["note"]) == (
        "done",
        "PRs merged (task-to-ticket map)",
    )


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_map_status_ticket_pr_on_row_and_station(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task-to-ticket map STATUS row puts its ticket and pull request on the task's row and its station."""
    stations = _stations(PROFILES[profile](tmp_path, monkeypatch))
    for task_id, ticket, pr in (
        ("W0.1", "OMN-33001", "OmniNode-ai/knowledge_base_internal#1573"),
        ("W0.2", "OMN-33002", "OmniNode-ai/knowledge_base_internal#1583"),
    ):
        station = stations[task_id]
        (item,) = station["items"]
        assert ticket in item["tickets"]
        assert ticket in station["tickets"]
        assert pr in item["prs"]
        assert pr in station["prs"]


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_text_mention_not_evidence(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A done TERMINAL that only names W1.2a in its text, even carrying the plan's epic ticket, does not build W1.2a."""
    w12a = _stations(PROFILES[profile](tmp_path, monkeypatch))["W1.2a"]
    assert (w12a["state"], w12a["items"][0]["note"]) == ("prep", "ready")


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_built_ahead_of_gate_left_out_of_built_percent(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1.1 merged, but its gate G1b reads fail: it shows the contract's label and does not count as built."""
    document = PROFILES[profile](tmp_path, monkeypatch)
    label = _compute_config().built_ahead_of_gate_label
    stations = _stations(document)
    w11 = stations["W1.1"]
    assert w11["state"] != "done"
    assert w11["items"][0]["note"] == f"{label} G1b: PRs merged"
    assert stations["G1b"]["state"] == "stop"
    # Built: W0.1 and W0.2 of five parts; W1.1 is left out.
    assert _line(document)["percent_built"] == 100 * 2 // 5
