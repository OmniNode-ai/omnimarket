# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the facts read with the retired script on 20 recorded ticks (OMN-23734, AC2).

``fixtures/recorded_facts_ticks.json`` holds 20 ticks recorded by running the script's own readers
(``read_launchctl``, ``read_controller_files``, ``read_watcher_and_floors``, ``read_open_history``,
``read_repo_status_units`` and the goal check's readers) against real files and scripted launchctl and
ps programs: each tick's files and the facts the script read. The test rebuilds the files and programs
under a temporary directory and reads them with the host source, so the same files give the same facts.
"""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_ruling_register import (
    EnumRulingVerdict,
    ModelRulingRegisterResult,
    ModelRulingVerdict,
)
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register import (
    read_register,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.handler_throughput_tick_facts import (
    HandlerThroughputTickFacts,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.tick_facts_ports import (
    ScriptedTickFactsSource,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelProcessOutcome,
    ModelThroughputTickFactsRequest,
    ModelThroughputTickFactsResult,
)

_RAW = (Path(__file__).parent / "fixtures" / "recorded_facts_ticks.json").read_text(
    encoding="utf-8"
)
_N = len(json.loads(_RAW)["ticks"])
RULING_STAMP = "2026-11-14T05:00:00Z"


@pytest.fixture
def short_root() -> Iterator[Path]:
    """A short root: the script cuts an error text at 120 characters, so the recorded texts hold only under a root as short as the recording's."""
    root = Path(tempfile.mkdtemp(prefix="tf", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _tick(index: int, root: Path) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = json.loads(
        _RAW.replace("{ROOT}", json.dumps(str(root))[1:-1])
    )["ticks"]
    return ticks[index]


def _program(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


#: The facts keys the retired script never read: the register keys are new (OMN-24478)
#: and are not in the recording, so every parity comparison ignores them.
_SCRIPT_ONLY_KEYS = ("ruling_register", "ruling_register_unread")


def script_facts(result: ModelThroughputTickFactsResult) -> dict[str, Any]:
    """The recorded facts: the result's facts without the two new register keys."""
    facts = result.facts.model_dump(exclude_unset=True)
    for key in _SCRIPT_ONLY_KEYS:
        facts.pop(key, None)
    return facts


def _build(tick: dict[str, Any], root: Path) -> ModelThroughputTickFactsRequest:
    for rel in tick["dirs"]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, text in tick["files"].items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    for name in ("landing", "state", "bin", "roots"):
        (root / name).mkdir(exist_ok=True)
    spec = tick["spec"]["launchctl"]
    if spec["mode"] != "missing":
        out = spec["stdout"]
        _program(
            root / "bin" / "launchctl",
            f"cat <<'EOF'\n{out}EOF\nexit 0\n"
            if spec["mode"] == "loaded"
            else "exit 1\n",
        )
    if spec["mode"] == "loaded" and spec["ps"] == "ok":
        _program(root / "bin" / "ps", f"echo '{spec['etime']}'\n")
    elif spec["mode"] == "loaded" and spec["ps"] == "fail":
        _program(root / "bin" / "ps", "exit 1\n")
    given = tick["request"]
    return ModelThroughputTickFactsRequest(
        now=given["now"],
        launchctl=str(root / "bin" / "launchctl"),
        ps=str(root / "bin" / "ps"),
        landing_state_dir=str(root / "landing"),
        watcher_state_path=str(root / "watcher" / "state.json")
        if given["watcher_given"]
        else None,
        floors_path=str(root / "floors.json"),
        state_dir=str(root / "state"),
        open_basis=given["open_basis"],
        overlay_roots=str(root / "roots"),
    )


@pytest.mark.parametrize("index", range(_N))
def test_facts_equal_what_the_script_read_parity(index: int, short_root: Path) -> None:
    tick = _tick(index, short_root)
    result = HandlerThroughputTickFacts().handle(_build(tick, short_root))
    assert script_facts(result) == tick["expected"]["facts"]
    expected_goal = tick["expected"]["goal"]
    goal = result.goal.model_dump(mode="json")
    if goal["scope"] is not None:
        goal["scope"] = {
            k: sorted(v) if isinstance(v, list) else v for k, v in goal["scope"].items()
        }
    assert {k: v for k, v in goal.items() if k in expected_goal} == expected_goal


def test_the_recording_holds_twenty_ticks_and_every_branch() -> None:
    ticks = json.loads(_RAW)["ticks"]
    assert len(ticks) == 20
    facts = [t["expected"]["facts"] for t in ticks]
    assert {f.get("launchctl") for f in facts} == {
        "loaded",
        "not_loaded",
        "unavailable",
    }
    for key in (
        "pid_etime",
        "ticks_error",
        "heartbeat_text",
        "state_json",
        "watcher_state",
        "watcher_read_error",
        "floors_per_repo",
        "floors_error",
        "land_like_operator",
    ):
        assert any(key in f for f in facts), key
    goals = [t["expected"]["goal"] for t in ticks]
    assert any(g["scope"] for g in goals)
    assert any(g["scope_unread"] for g in goals)
    assert any(g["admissions_unread"] for g in goals)
    assert any(g["pr_reasons"] for g in goals)
    assert any(g["pr_reasons_unread"] for g in goals)


def test_a_planted_change_to_one_file_turns_the_parity_red(short_root: Path) -> None:
    index = next(
        i
        for i, t in enumerate(json.loads(_RAW)["ticks"])
        if "watcher_state" in t["expected"]["facts"]
    )
    tick = _tick(index, short_root)
    request = _build(tick, short_root)
    watcher = short_root / "watcher" / "state.json"
    state = json.loads(watcher.read_text(encoding="utf-8"))
    state["prs"]["omnimarket#1"]["facts"]["author"] = "planted-author"
    watcher.write_text(json.dumps(state), encoding="utf-8")
    result = HandlerThroughputTickFacts().handle(request)
    assert script_facts(result) != tick["expected"]["facts"]


def test_golden_a_scripted_host_gives_the_loaded_controller_facts() -> None:
    controller = "ai.omninode.landing-controller"
    source = ScriptedTickFactsSource(
        files={
            "/l/ticks.jsonl": '{"n": 1}\n{"n": 2, "pr_reasons": {"o/r#1": "queued"}}\n',
            "/l/heartbeat": "2026-11-14T05:00:00Z pid=1\n",
            "/l/state.json": '{"a": 1}',
            "/w/state.json": '{"prs": {"r#1": {"facts": {"author": "alice"}}, "r#2": {"facts": {}}}}',
            "/f.json": '{"default_per_hour": 1, "per_repo": {"r": 3}}',
        },
        programs={
            "launchctl": ModelProcessOutcome(
                returncode=0, stdout=f'{{"Label" = "{controller}"; "PID" = 4242;}};'
            ),
            "ps": ModelProcessOutcome(returncode=0, stdout=" 12:34\n"),
        },
    )
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        watcher_state_path="/w/state.json",
        floors_path="/f.json",
        state_dir="/s",
        open_basis="b",
    )
    facts = (
        HandlerThroughputTickFacts(source)
        .handle(request)
        .facts.model_dump(exclude_unset=True)
    )
    assert facts["launchctl"] == "loaded"
    assert facts["pid_etime"] == "12:34"
    assert [t["n"] for t in facts["ticks"]] == [2, 1]
    assert facts["floors_per_repo"] == {"r": 3.0}
    assert facts["land_like_operator"] == ["alice"]
    assert facts["open_history"] == []
    assert facts["repo_status"] == {
        "window_minutes": 60,
        "open_units": [],
    }


def test_error_chain_every_unreadable_source_is_its_error_text_not_a_raise() -> None:
    source = ScriptedTickFactsSource(
        files={
            "/w/state.json": "{",
            "/f.json": '{"default_per_hour": -1, "per_repo": {}}',
        },
        programs={"launchctl": "launchctl timed out after 2 seconds"},
        unreadable={"/l/ticks.jsonl": "Input/output error"},
    )
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        watcher_state_path="/w/state.json",
        floors_path="/f.json",
        state_dir="/s",
        open_basis="b",
    )
    result = HandlerThroughputTickFacts(source).handle(request)
    facts = result.facts.model_dump(exclude_unset=True)
    assert facts["launchctl"] == "unavailable"
    assert facts["launchctl_error"] == "launchctl timed out after 2 seconds"
    assert facts["ticks"] == []
    assert facts["ticks_error"] == "Input/output error"
    assert facts["watcher_read_error"].startswith("Expecting property name")
    assert (
        facts["floors_error"]
        == "cannot read floors /f.json: default_per_hour must be a nonnegative finite number"
    )
    assert "land_like_operator" not in facts
    assert result.goal.scope is None
    assert result.goal.scope_unread.startswith(
        "no goal_scope/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS=unset"
    )
    assert result.goal.pr_reasons is None
    assert result.goal.pr_reasons_unread == "ticks.jsonl: Input/output error".replace(
        "ticks.jsonl", "/l/ticks.jsonl"
    )


def test_error_chain_no_floors_path_is_a_floors_error() -> None:
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        state_dir="/s",
        open_basis="b",
    )
    facts = (
        HandlerThroughputTickFacts(ScriptedTickFactsSource())
        .handle(request)
        .facts.model_dump(exclude_unset=True)
    )
    assert (
        facts["floors_error"] == "cannot read floors: the request names no floors_path"
    )
    assert facts["launchctl"] == "unavailable"


# ---- the standing-rulings register (OMN-24478) ------------------------------------------------------


class _RegisterSource:
    """A register source answered from a fixed result, or raising its error: the handler's
    injected ``ruling_register`` port."""

    def __init__(
        self,
        result: ModelRulingRegisterResult | None = None,
        error: Exception | None = None,
        sink_errors: tuple[str, ...] = (),
    ) -> None:
        self._result = result
        self._error = error
        self._sink_errors = sink_errors

    def read(
        self, overlay_roots: str
    ) -> tuple[ModelRulingRegisterResult, tuple[str, ...]]:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, self._sink_errors


def register_result(*row_ids: str) -> ModelRulingRegisterResult:
    """A judged register holding one UNREAD verdict per named row."""
    verdicts = tuple(
        ModelRulingVerdict(
            row_id=row_id,
            ruling=RULING_STAMP,
            owner="example-lane",
            verdict=EnumRulingVerdict.UNREAD,
            detail="no reading fixture",
            why=f"ruling {RULING_STAMP} not running (UNREAD: no reading fixture)",
            fix=f"fix {row_id}",
        )
        for row_id in row_ids
    )
    return ModelRulingRegisterResult(
        correlation_id="corr-parity-register",
        passed=False,
        verdicts=verdicts,
        true_count=0,
        false_count=0,
        unread_count=len(verdicts),
    )


def register_request(**overrides: Any) -> ModelThroughputTickFactsRequest:
    return ModelThroughputTickFactsRequest(
        now=RULING_STAMP,
        landing_state_dir="/l",
        state_dir="/s",
        open_basis="b",
        **overrides,
    )


def install_register_overlays(root: Path) -> str:
    """Both overlays a register read needs under one root (the register effect's CLI tests
    write the same pair): the rows and the in-memory read route. Returns the root as a string."""
    compute = root / "node_ruling_register_compute"
    compute.mkdir(parents=True, exist_ok=True)
    (compute / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "max_reading_age_seconds": 600,
                "allowed_commands": ["example-command"],
                "rows": [
                    {
                        "row_id": "row-quiet",
                        "ruling": RULING_STAMP,
                        "must_be_true": "the example ruling holds",
                        "read": {
                            "host": "local",
                            "argv": ["example-command"],
                            # exit_zero names the nonzero code that reads FALSE.
                            "ok_exit_codes": [0, 1],
                            "expect": {"kind": "exit_zero"},
                        },
                        "owner": "example-lane",
                        "fix": "fix row-quiet",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    effect = root / "node_ruling_register_effect"
    effect.mkdir(parents=True, exist_ok=True)
    (effect / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "inmemory",
                # under the route schema's ceilings, the total bounding the reads' sum
                "timeout_seconds": 10,
                "total_timeout_seconds": 20,
                "remote_argv_prefix": ["ssh", "-o", "BatchMode=yes", "{target}"],
                "local_hosts": ["local"],
                "host_targets": {"host-a": "192.0.2.10"},
                "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
            }
        ),
        encoding="utf-8",
    )
    return str(root)


def test_a_register_source_the_facts_carry_the_register(short_root: Path) -> None:
    judged = register_result("row-1", "row-2")
    facts = (
        HandlerThroughputTickFacts(
            ScriptedTickFactsSource(),
            ruling_register=_RegisterSource(result=judged),
        )
        .handle(register_request(overlay_roots=str(short_root / "roots")))
        .facts
    )
    assert facts.ruling_register is judged
    dump = facts.model_dump(exclude_unset=True)
    assert dump["ruling_register"]["correlation_id"] == "corr-parity-register"
    assert dump["ruling_register"]["unread_count"] == 2
    assert "ruling_register_unread" not in dump


def test_a_register_source_that_refuses_is_the_unread_reason_never_a_raise() -> None:
    facts = (
        HandlerThroughputTickFacts(
            ScriptedTickFactsSource(),
            ruling_register=_RegisterSource(
                error=NodeOverlayError("no node_ruling_register_compute overlay")
            ),
        )
        .handle(register_request())
        .facts
    )
    assert facts.ruling_register is None
    assert (
        facts.ruling_register_unread
        == "NodeOverlayError: no node_ruling_register_compute overlay"
    )
    dump = facts.model_dump(exclude_unset=True)
    assert "ruling_register" not in dump
    assert dump["ruling_register_unread"].startswith("NodeOverlayError: ")


def test_the_default_source_reads_the_register_from_the_overlay_roots(
    short_root: Path,
) -> None:
    roots = install_register_overlays(short_root / "roots")
    facts = (
        HandlerThroughputTickFacts(ScriptedTickFactsSource())
        .handle(register_request(overlay_roots=roots))
        .facts
    )
    judged = facts.ruling_register
    assert judged is not None
    assert [verdict.row_id for verdict in judged.verdicts] == ["row-quiet"]
    assert all(
        verdict.verdict is EnumRulingVerdict.UNREAD for verdict in judged.verdicts
    )
    assert all("no reading fixture" in verdict.detail for verdict in judged.verdicts)
    assert "ruling_register_unread" not in facts.model_dump(exclude_unset=True)


def test_the_default_source_searches_the_installed_roots_after_the_overlay_roots(
    short_root: Path,
) -> None:
    """The register overlays only under an installed root: the source reads them from the
    os.pathsep list the goal reader searches (overlay_roots first, then installed_roots)."""
    (short_root / "roots").mkdir()
    installed = install_register_overlays(short_root / "installed")
    facts = (
        HandlerThroughputTickFacts(ScriptedTickFactsSource())
        .handle(
            register_request(
                overlay_roots=str(short_root / "roots"),
                installed_roots=[installed],
            )
        )
        .facts
    )
    assert facts.ruling_register is not None
    assert facts.ruling_register.unread_count == 1
    assert "ruling_register_unread" not in facts.model_dump(exclude_unset=True)


def test_read_register_names_the_failed_journal_write_and_still_reads_every_row(
    short_root: Path,
) -> None:
    """A journal path under a regular file cannot be written: the row's event is lost and
    comes back as its sink error, while the row itself still reads (UNREAD under the
    in-memory route, which holds no fixtures) and is judged."""
    roots = install_register_overlays(short_root / "roots")
    blocked = short_root / "blocked"
    blocked.write_text("a regular file", encoding="utf-8")
    judged, sink_errors = read_register(
        environ={
            "ONEX_SKILL_OVERLAY_ROOTS": roots,
            "ONEX_RULING_REGISTER_JOURNAL": str(blocked / "journal.jsonl"),
        }
    )
    assert sink_errors == ("row-quiet: FileExistsError",)
    assert [verdict.row_id for verdict in judged.verdicts] == ["row-quiet"]
    assert judged.unread_count == 1
    assert not blocked.joinpath("journal.jsonl").exists()
