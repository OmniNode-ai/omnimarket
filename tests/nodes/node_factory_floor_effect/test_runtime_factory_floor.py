# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The thin command line and its source loaders: determinism, the contract-declared output path, refusals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.runtime_factory_floor import main
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_contract,
    load_linear,
    load_watcher,
    parse_ledger_text,
    write_result,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACT = NODES / "node_factory_floor_effect/contract.yaml"
COMPUTE_CONTRACT = NODES / "node_factory_floor_compute/contract.yaml"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

MANIFEST = {
    "title": "Wave plan (tasks)",
    "epic": "OMN-100",
    "tasks": [
        {
            "id": "W0.1",
            "title": "W0.1 one",
            "wave": "0",
            "kind": "lane",
            "depends_on": [],
        },
        {
            "id": "W0.2",
            "title": "W0.2 two",
            "wave": "0",
            "kind": "lane",
            "depends_on": [],
        },
        {
            "id": "G0",
            "title": "G0 gate",
            "wave": "0",
            "kind": "gate",
            "depends_on": ["W0.1", "W0.2"],
        },
        {
            "id": "P1",
            "title": "P1 next",
            "wave": "1a",
            "kind": "adopted",
            "depends_on": ["G0"],
        },
    ],
}

LEDGER = "\n".join(
    [
        "ROWS 5 newest=2026-10-31T11:55:00Z",
        "L1 2026-10-31T11:40:00Z | CLAIM | lane=lane-a | ticket=OMN-100 | task=W0.2 | est ~2h | claim text",
        "L2 2026-10-31T11:41:00Z | HOLD | lane=lane-a | id=2026-10-31T11:41:00Z-lane-a | to=lane-a | ticket=OMN-100 | until=2026-10-31T15:00:00Z | reason=andon:gate:0:W0.2 | Andon stop on the line.",
        "L3 2026-10-31T11:50:00Z | STATUS | lane=lane-a | ticket=OMN-100 | state=gate | subject=0 | verdict=fail | Gate G0 FAIL.",
        "L4 2026-10-31T11:55:00Z | MSG | from=lane-b | to=lane-a | ticket=OMN-100 | a message",
        "L5 2026-10-31T11:20:00Z | MSG | from=lane-b | to=lane-a | an older message",
        "not a row",
    ]
)


def _write(path: Path, content: str | dict[str, Any] | list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
    )
    return path


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    home, kb = tmp_path / "home", tmp_path / "kb"
    scratch = home / ".claude_scratch"
    _write(
        scratch / "pr-watcher" / "state.json",
        {
            "last_tick": "2026-10-31T11:57:00Z",
            "prs": {
                "kb#1": {
                    "facts": {
                        "state": "MERGED",
                        "title": "W0.1 one",
                        "head_ref": "b",
                        "body": "",
                    },
                    "cls": "merged",
                },
                "kb#2": {
                    "facts": {
                        "state": "OPEN",
                        "title": "W0.2 two",
                        "head_ref": "c",
                        "body": "OMN-100",
                        "draft": True,
                    },
                    "cls": "held-red",
                },
                "kb#3": {"facts": {"state": "WEIRD"}},
            },
            "merges": {
                "kb#1": {
                    "merged_at": "2026-10-31T11:30:00Z",
                    "title": "W0.1 one",
                    "head_ref": "b",
                }
            },
        },
    )
    _write(
        scratch / "plans-today" / "2026-10-31" / "plans-today.json",
        {
            "plans": [
                {
                    "path": "beta/plans/2026-10-01-old-plan.md",
                    "tickets": ["OMN-7"],
                    "prs": [],
                    "tasks": [
                        {
                            "label": "T1",
                            "text": "first",
                            "tickets": ["OMN-7"],
                            "prs": [],
                            "state": "merged",
                            "owner": "NONE",
                        },
                        {
                            "label": "T2",
                            "text": "second",
                            "tickets": ["OMN-8"],
                            "prs": [],
                            "state": "not-started",
                            "owner": "NONE",
                        },
                    ],
                }
            ]
        },
    )
    _write(
        scratch / "plans-today" / "linear-2026-10-30.txt",
        "OMN-7 In Progress 2026-10-30T09:00:00Z\n",
    )
    _write(
        scratch / "plans-today" / "linear-2026-10-31.txt",
        "OMN-7 Done 2026-10-31T09:00:00Z\nOMN-8 In Progress 2026-10-31T09:00:00Z priority=High\n",
    )
    _write(
        scratch / "lab-run" / "hosts.json",
        [
            {
                "host_name": "h201",
                "cores": 8,
                "load1": 0.5,
                "mem_available_bytes": 8589934592,
                "running_units": 1,
                "max_units": 3,
                "advertised_at": "2026-10-31T11:59:55Z",
                "cadence_seconds": 10,
            }
        ],
    )
    _write(
        scratch / "friction" / "friction-report.json",
        {
            "classes": [
                {
                    "class": "gate-refusal",
                    "occurrences": 3,
                    "cost_minutes": 12.5,
                    "recurring": True,
                    "tickets": ["OMN-1"],
                    "last_seen": "2026-10-31T10:00:00Z",
                },
                {"occurrences": 1},
            ]
        },
    )
    plans = kb / "beta" / "plans"
    _write(plans / "2026-10-31-wave-plan.md", "# Wave plan\n\nOMN-100 and repo#5.\n")
    _write(plans / "2026-10-31-wave-plan.readable.md", "# readable companion\n")
    _write(plans / "2026-10-31-wave-plan.tasks.yaml", yaml.safe_dump(MANIFEST))
    _write(plans / "2026-10-01-old-plan.md", "# Old plan\n")
    _write(plans / "2026-09-01-other.md", "# Other\n")
    return {
        "home": home,
        "kb": kb,
        "ledger": _write(tmp_path / "ledger.txt", LEDGER),
        "tmp": tmp_path,
    }


def _argv(world: dict[str, Path], *extra: str) -> list[str]:
    return [
        *("--home", str(world["home"])),
        *("--kb", str(world["kb"])),
        *("--now", "2026-10-31T12:00:00Z"),
        *("--ledger-rows-file", str(world["ledger"])),
        *extra,
    ]


def _output(world: dict[str, Path]) -> Path:
    return world["home"] / load_contract(CONTRACT).runtime.output.relpath


# -------------------------------------------------------------------------------------- the CLI


def test_determinism_or_cli_writes_json_to_the_contract_declared_path(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(world, "--plan", "beta/plans/2026-10-")) == 0
    target = _output(world)
    assert capsys.readouterr().out.strip() == str(target)
    assert target.parent.name == "factory-floor"
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema_id"] == "omni.factory-floor/1"
    lines = {line["line_id"]: line for line in document["lines"]}
    assert set(lines) == {"2026-10-31-wave-plan", "2026-10-01-old-plan"}
    wave = lines["2026-10-31-wave-plan"]
    assert wave["source"] == "manifest"
    assert wave["status_pill"] == "stopped"
    assert wave["andon"]["flag"] is True
    assert wave["andon"]["causes"] == [
        "HOLD 2026-10-31T11:41:00Z-lane-a: andon:gate:0:W0.2",
        "GATE G0 failed",
    ]
    stations = {st["id"]: st for st in wave["stations"]}
    assert stations["0"]["state"] == "stop"
    assert stations["1a"]["feeders"] == ["0"]
    old = lines["2026-10-01-old-plan"]
    assert old["source"] == "plan-task-table"
    assert [i["state"] for i in old["stations"][0]["items"]] == ["done", "prep"]
    floor = document["floor"]
    assert floor["bays"] == [
        {"host": "h201", "slots": 3, "running": 1, "free": 2, "stale": False}
    ]
    assert [s["pr"] for s in floor["shipped"]] == ["kb#1"]
    assert [(w["pr"], w["detail"]) for w in floor["waiting"]] == [("kb#2", "draft")]
    assert [s["name"] for s in floor["stoppages"]] == ["gate-refusal"]
    assert (
        document["sources"]["linear_issues"] == 2
    )  # the newest snapshot, not the older one


def test_determinism_or_cli_two_runs_write_identical_bytes(
    world: dict[str, Path],
) -> None:
    assert main(_argv(world, "--plan", "wave-plan")) == 0
    first = _output(world).read_bytes()
    assert main(_argv(world, "--plan", "wave-plan")) == 0
    assert _output(world).read_bytes() == first


def test_determinism_or_cli_overlay_selects_the_plans_when_no_flag_is_given(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _write(world["tmp"] / "overlay.yaml", "plan_path_contains: [old-plan]\n")
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_OVERLAY", str(overlay))
    assert main(_argv(world)) == 0
    lines = json.loads(_output(world).read_text(encoding="utf-8"))["lines"]
    assert [line["line_id"] for line in lines] == ["2026-10-01-old-plan"]


def test_determinism_or_cli_refuses_without_a_plan_selection(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_FACTORY_FLOOR_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main(_argv(world)) == 2
    assert "REFUSED factory-floor" in capsys.readouterr().err
    assert not _output(world).exists()


def test_determinism_or_cli_refuses_a_stale_or_missing_watcher_and_an_unset_home(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    late = ["--now", "2026-10-31T14:00:00Z"]
    argv = _argv(world, "--plan", "wave-plan")
    stale = argv[: argv.index("--now")] + late + argv[argv.index("--now") + 2 :]
    assert main(stale) == 2
    assert "minutes old" in capsys.readouterr().err
    (world["home"] / ".claude_scratch" / "pr-watcher" / "state.json").unlink()
    assert main(argv) == 2
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert main(["--kb", str(world["kb"]), "--plan", "x"]) == 2
    assert "OMNI_HOME is not set" in capsys.readouterr().err


def test_determinism_or_cli_refuses_an_invalid_manifest(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        world["kb"] / "beta" / "plans" / "2026-10-31-wave-plan.tasks.yaml",
        "tasks: [{title: no id}]\n",
    )
    assert main(_argv(world, "--plan", "wave-plan")) == 2
    assert "tasks.yaml" in capsys.readouterr().err


# --------------------------------------------------------------------------------- the loaders


def test_determinism_or_cli_ledger_parse_reads_cells_and_keeps_one_row_per_quiet_kind() -> (
    None
):
    rows = parse_ledger_text(LEDGER)
    kinds = [r.kind for r in rows]
    assert kinds == [
        "CLAIM",
        "HOLD",
        "STATUS",
        "MSG",
    ]  # the older MSG of lane-b is dropped
    claim, hold, status, msg = rows
    assert (claim.task, claim.tickets, claim.text) == (
        "W0.2",
        ("OMN-100",),
        "claim text",
    )
    assert hold.row_id == "2026-10-31T11:41:00Z-lane-a"
    assert hold.reason == "andon:gate:0:W0.2"
    assert hold.until is not None
    assert hold.until.hour == 15
    assert (status.state, status.subject, status.verdict) == ("gate", "0", "fail")
    assert msg.lane == "lane-b"
    assert msg.ts.isoformat() == "2026-10-31T11:55:00+00:00"


def test_determinism_or_cli_linear_reads_the_compact_and_the_json_forms(
    tmp_path: Path,
) -> None:
    compact = _write(
        tmp_path / "l.txt",
        "OMN-1 In Progress 2026-10-31T09:00:00Z priority=High\nOMN-2 Done 2026-10-31T09:00:00Z\nOMN-3 Backlog 2026-10-31T09:00:00Z\n",
    )
    assert [(i.id, i.status_type) for i in load_linear(compact)] == [
        ("OMN-1", "started"),
        ("OMN-2", "completed"),
        ("OMN-3", ""),
    ]
    as_json = _write(
        tmp_path / "l.json",
        {
            "issues": [
                {"id": "OMN-9", "status": "Done", "statusType": "completed"},
                {"id": "nope"},
            ]
        },
    )
    assert [(i.id, i.status_type) for i in load_linear(as_json)] == [
        ("OMN-9", "completed")
    ]
    assert load_linear(None) == ()
    assert load_linear(tmp_path / "missing.txt") == ()


def test_determinism_or_cli_watcher_staleness_is_judged_against_the_supplied_time(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "w.json",
        {"last_tick": "2026-10-31T11:00:00Z", "prs": {}, "merges": {}},
    )
    assert load_watcher(path, NOW, 90).last_tick.hour == 11
    with pytest.raises(SourceRefusedError, match=r"60\.0 minutes old"):
        load_watcher(path, NOW, 30)


def test_determinism_or_cli_write_result_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "out.json"
    write_result(target, {"a": 1})
    write_result(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["out.json"]


def test_determinism_or_cli_the_contract_declares_every_runtime_setting() -> None:
    contract = load_contract(CONTRACT)
    assert contract.runtime.output.relpath.endswith("factory-floor.json")
    assert contract.runtime.sources.watcher_relpath.endswith("state.json")
    config = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))["config"]
    assert config["schema_id"] == "omni.factory-floor/1"
