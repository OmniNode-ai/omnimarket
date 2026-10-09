# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters, against a scripted skills directory and scratch files (OMN-20668)."""

from __future__ import annotations

import json
import os
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillPortError
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    local_lab_fill_adapters as adapters,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalApprovedWork,
    LocalBriefBlocks,
    LocalLaneLauncher,
    LocalLedgerReader,
    LocalLiveChecks,
    LocalOwnerReader,
    LocalPlacementReader,
    LocalReceiptReader,
    LocalResultWriter,
    appender_from_environment,
    resolve_skills_dir,
)


def _skills(
    root: Path, *, runner_body: str = "print('DETACHED receipt=/r.json')"
) -> Path:
    skills = root / "plugin" / "skills"
    (skills / "remote-lane/scripts").mkdir(parents=True)
    (skills / "merge-drain/scripts").mkdir(parents=True)
    (skills / "lane-brief/scripts").mkdir(parents=True)
    (root / "plugin" / "scripts").mkdir(parents=True)
    (skills / "remote-lane/scripts/onex_remote_lane.py").write_text(runner_body + "\n")
    (skills / "merge-drain/scripts/landing_placement.py").write_text("")
    return skills


def test_skills_dir_is_the_declared_one_when_it_holds_the_runner(
    tmp_path: Path,
) -> None:
    skills = _skills(tmp_path)
    assert (
        resolve_skills_dir({"HOME": str(tmp_path), "ONEX_OMNI_SKILLS_DIR": str(skills)})
        == skills
    )


def test_skills_dir_without_the_runner_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(LabFillPortError, match="ONEX_OMNI_SKILLS_DIR"):
        resolve_skills_dir(
            {"HOME": str(tmp_path), "ONEX_OMNI_SKILLS_DIR": str(tmp_path / "empty")}
        )


def test_skills_dir_prefers_the_newest_installed_version(tmp_path: Path) -> None:
    base = tmp_path / ".claude/plugins/cache/omninode-internal/omni"
    for version in ("0.4.9", "0.4.100", "0.4.20"):
        skills = base / version / "skills"
        (skills / "remote-lane/scripts").mkdir(parents=True)
        (skills / "merge-drain/scripts").mkdir(parents=True)
        (skills / "remote-lane/scripts/onex_remote_lane.py").write_text("")
        (skills / "merge-drain/scripts/landing_placement.py").write_text("")
    assert resolve_skills_dir({"HOME": str(tmp_path)}).parent.name == "0.4.100"


def test_ledger_reader_finds_this_fires_status_row_and_only_that(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "L.md"
    ledger.write_text(
        "2026-10-09T00:40:00Z | STATUS | lane=lab-fill | run=2026-10-09T0020Z | x=1\n"
        "2026-10-09T01:00:05Z | STATUS | lane=lab-fill | run=2026-10-09T0100Z | x=2\n"
        "2026-10-09T01:00:06Z | STATUS | lane=other | run=2026-10-09T0200Z | x=3\n"
        "2026-10-09T01:00:07Z | CLAIM | lane=lab-fill | run=2026-10-09T0300Z | x=4\n"
    )
    reader = LocalLedgerReader()
    assert (
        reader.status_row_time(str(ledger), "2026-10-09T0100Z")
        == "2026-10-09T01:00:05Z"
    )
    assert (
        reader.status_row_time(str(ledger), "2026-10-09T0200Z") is None
    )  # another lane's row
    assert (
        reader.status_row_time(str(ledger), "2026-10-09T0300Z") is None
    )  # not a STATUS row
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        reader.status_row_time(str(tmp_path / "missing.md"), "k")


def test_approved_work_depth_and_row_by_id(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({"version": 1, "rows": [{"id": "a"}, {"id": "b"}, {"id": "b"}]})
    )
    work = LocalApprovedWork()
    assert work.depth(str(path)) == 3
    assert dict(work.row(str(path), "a")) == {"id": "a"}
    with pytest.raises(LabFillPortError, match="missing or duplicate row"):
        work.row(str(path), "b")
    with pytest.raises(LabFillPortError, match="missing or duplicate row"):
        work.row(str(path), "zzz")


@pytest.mark.parametrize(
    "content",
    [
        "{",
        json.dumps({"version": 2, "rows": []}),
        json.dumps({"version": 1, "rows": {}}),
    ],
)
def test_approved_work_depth_is_none_for_an_unreadable_or_invalid_list(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "a.json"
    path.write_text(content)
    assert LocalApprovedWork().depth(str(path)) is None
    assert LocalApprovedWork().depth(str(tmp_path / "missing.json")) is None


def test_receipt_reader_and_result_writer(tmp_path: Path) -> None:
    (tmp_path / "r.json").write_text(json.dumps({"host": "h1"}))
    (tmp_path / "bad.json").write_text("[1]")
    reader = LocalReceiptReader()
    assert reader.read(str(tmp_path / "r.json")) == {"host": "h1"}
    assert reader.read(str(tmp_path / "bad.json")) is None
    assert reader.read(str(tmp_path / "nope.json")) is None
    target = tmp_path / "deep" / "dir" / "fire.json"
    LocalResultWriter().write(str(target), {"b": 2, "a": 1})
    assert json.loads(target.read_text()) == {"a": 1, "b": 2}
    assert [p.name for p in target.parent.iterdir()] == ["fire.json"]


def test_lane_runner_passes_args_env_and_reports_the_exit(tmp_path: Path) -> None:
    skills = _skills(
        tmp_path,
        runner_body="import os, sys; print('DETACHED receipt=/r.json', os.environ['ONEX_LEDGER_PATH'], *sys.argv[1:]); sys.exit(0)",
    )
    out = LocalLaneLauncher(skills).run(
        ["run", "--lane", "x"], env={"ONEX_LEDGER_PATH": "/L"}, timeout_s=20
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "DETACHED receipt=/r.json /L run --lane x"


def test_lane_runner_times_out_as_124(tmp_path: Path) -> None:
    skills = _skills(tmp_path, runner_body="import time; time.sleep(30)")
    out = LocalLaneLauncher(skills).run(["run"], env={}, timeout_s=1)
    assert out.returncode == 124


def test_brief_blocks_validate_the_rules_then_append_delegation_and_lab(
    tmp_path: Path,
) -> None:
    skills = _skills(tmp_path)
    (skills.parent / "scripts/lane_rules_block.py").write_text(
        "print('# Standing rules\\nAuthority: ok MARK')"
    )
    (skills / "lane-brief/scripts/lane_brief.py").write_text(
        textwrap.dedent(
            """
            RULES_BLOCK_HEAD = "# Standing rules"
            DISPATCH_RULES = {"Authority": ["MARK"]}
            def unfenced_prose(text): return text
            def delegation_block(a): return "DELEGATION " + a.ticket
            def lab_block(a): return "LAB " + a.ticket
            """
        )
    )
    brief = tmp_path / "b.md"
    brief.write_text("# Lane\n")
    tail = LocalBriefBlocks(skills).blocks(ticket="OMN-7", brief_path=str(brief))
    assert tail.endswith("DELEGATION OMN-7\n\nLAB OMN-7\n")
    assert "Authority: ok MARK" in tail


def test_brief_blocks_refuse_rules_that_lack_a_required_marker(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills.parent / "scripts/lane_rules_block.py").write_text(
        "print('# Standing rules\\nAuthority: ok')"
    )
    (skills / "lane-brief/scripts/lane_brief.py").write_text(
        'RULES_BLOCK_HEAD = "# Standing rules"\nDISPATCH_RULES = {"Authority": ["MARK"]}\n'
        "def unfenced_prose(t): return t\ndef delegation_block(a): return ''\ndef lab_block(a): return ''\n"
    )
    brief = tmp_path / "b.md"
    brief.write_text("# Lane\n")
    with pytest.raises(LabFillPortError, match="lack Authority MARK"):
        LocalBriefBlocks(skills).blocks(ticket="OMN-7", brief_path=str(brief))


def test_pr_claims_parse_the_registry_listing(tmp_path: Path) -> None:
    cli = tmp_path / "cli.py"
    cli.write_text(
        "import sys\nassert sys.argv[1] == 'list'\n"
        "print('  OmniNode-ai/repo#12 | lane: lab-fill-x | action: fix')\nprint('noise')\nprint('other#3|lane:peer')\n"
    )
    assert LocalOwnerReader().pr_claims(str(cli)) == {
        "repo#12": "lab-fill-x",
        "other#3": "peer",
    }
    cli.write_text("import sys; sys.exit(3)")
    with pytest.raises(LabFillPortError, match="exit 3"):
        LocalOwnerReader().pr_claims(str(cli))


def _watcher(path: Path, *, age_min: float = 1, schema: int = 1) -> None:
    tick = (datetime.now(UTC) - timedelta(minutes=age_min)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "last_tick": tick,
                "prs": {
                    "a": {
                        "facts": {
                            "repo": "OmniNode-ai/omnimarket",
                            "number": 5,
                            "title": "feat(OMN-7): x",
                            "state": "MERGED",
                        }
                    },
                    "b": {
                        "facts": {
                            "repo": "OmniNode-ai/onex_change_control",
                            "number": 6,
                            "title": "chore(OMN-7): y",
                            "state": "MERGED",
                        }
                    },
                    "c": {
                        "facts": {
                            "repo": "OmniNode-ai/omnimarket",
                            "number": 7,
                            "title": "evidence(OMN-8): z",
                            "merged_at": "x",
                        }
                    },
                    "d": {
                        "facts": {
                            "repo": "OmniNode-ai/omnimarket",
                            "number": 8,
                            "title": "feat(OMN-9): open",
                            "state": "OPEN",
                        }
                    },
                },
                "merges": {
                    "m": {
                        "repo": "OmniNode-ai/omnimarket",
                        "number": 4,
                        "title": "fix(OMN-7): old",
                        "merged_at": "x",
                    }
                },
            }
        )
    )


def test_watcher_merged_reads_implementation_merges_only(tmp_path: Path) -> None:
    state = tmp_path / "w.json"
    _watcher(state)
    assert LocalOwnerReader().watcher_merged(str(state)) == {
        "OMN-7": ["omnimarket#4", "omnimarket#5"]
    }


@pytest.mark.parametrize(("age_min", "schema"), [(20.0, 1), (1.0, 2)])
def test_watcher_merged_refuses_a_stale_or_unknown_snapshot(
    tmp_path: Path, age_min: float, schema: int
) -> None:
    state = tmp_path / "w.json"
    _watcher(state, age_min=age_min, schema=schema)
    with pytest.raises(LabFillPortError, match="unreadable-from-snapshot"):
        LocalOwnerReader().watcher_merged(str(state))


def test_claims_without_omnibase_internal_are_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIBASE_INTERNAL_HOME", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
    with pytest.raises(LabFillPortError, match="OMNIBASE_INTERNAL_HOME"):
        LocalOwnerReader().claims("/l", ["OMN-1"])


def test_claims_bridge_failure_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project directory with no omnibase_internal in it: the bridge exits nonzero and says so.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "empty"\nversion = "0"\n'
    )
    monkeypatch.setenv("OMNIBASE_INTERNAL_HOME", str(tmp_path))
    with pytest.raises(LabFillPortError, match="claim bridge"):
        LocalOwnerReader().claims(str(tmp_path / "L.md"), ["OMN-1"])


def _linear(monkeypatch: pytest.MonkeyPatch, node: dict[str, object] | None) -> None:
    nodes = [] if node is None else [node]
    monkeypatch.setattr(
        adapters,
        "_graphql",
        lambda _query, _timeout_s=20.0: {"issues": {"nodes": nodes}},
    )


def test_live_checks_assignment_and_completion_and_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = LocalLiveChecks()
    monkeypatch.setattr(
        LocalLiveChecks, "_fences", staticmethod(lambda _ledger: {"OMN-9"})
    )
    kwargs = {"pr": "", "operator_id": "me", "ledger_path": "/l"}
    _linear(
        monkeypatch,
        {
            "identifier": "OMN-1",
            "state": {"type": "started"},
            "assignee": {"id": "someone"},
        },
    )
    assert (
        checks.skip_reason(ticket="OMN-1", kind="ticket", **kwargs)[0]
        == "assigned-other"
    )
    _linear(
        monkeypatch,
        {"identifier": "OMN-1", "state": {"type": "completed"}, "assignee": None},
    )
    assert checks.skip_reason(ticket="OMN-1", kind="wiring", **kwargs)[0] == "done"
    assert checks.skip_reason(ticket="OMN-1", kind="ticket", **kwargs) == (
        "",
        "",
    )  # only approved kinds skip on completion
    _linear(
        monkeypatch,
        {"identifier": "OMN-9", "state": {"type": "started"}, "assignee": {"id": "me"}},
    )
    assert checks.skip_reason(ticket="OMN-9", kind="ticket", **kwargs)[0] == "fenced"
    _linear(monkeypatch, None)
    with pytest.raises(LabFillPortError, match="not found"):
        checks.skip_reason(ticket="OMN-2", kind="ticket", **kwargs)


def test_live_checks_fences_that_cannot_be_read_stop_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(ledger: str) -> set[str]:
        raise LabFillPortError("onex-triage-fences: FileNotFoundError")

    monkeypatch.setattr(LocalLiveChecks, "_fences", staticmethod(broken))
    _linear(
        monkeypatch,
        {"identifier": "OMN-1", "state": {"type": "started"}, "assignee": None},
    )
    reason = LocalLiveChecks().skip_reason(
        ticket="OMN-1", kind="ticket", pr="", operator_id="me", ledger_path="/l"
    )
    assert reason[0] == "fences-unreadable"


def test_live_checks_pr_hold_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "w.json"
    state.write_text(
        json.dumps(
            {
                "prs": {
                    "a": {
                        "facts": {
                            "repo": "omnimarket",
                            "number": 5,
                            "labels": ["hold:wip"],
                        }
                    },
                    "b": {
                        "facts": {
                            "repo": "omnimarket",
                            "number": 6,
                            "labels": ["hold:auto-merge"],
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(state))
    assert LocalLiveChecks._pr_hold("omnimarket#5") == ("held", "hold:wip")
    assert LocalLiveChecks._pr_hold("omnimarket#6") == ("", "")
    assert LocalLiveChecks._pr_hold("omnimarket#99")[0] == "pr-state-unreadable"
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(tmp_path / "missing.json"))
    assert LocalLiveChecks._pr_hold("omnimarket#5")[0] == "pr-state-unreadable"


def test_graphql_without_a_key_names_the_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(LabFillPortError, match="LINEAR_API_KEY is missing"):
        adapters._graphql("{x}")


def test_appender_is_declared_by_environment_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_LAB_FILL_LEDGER_BUS_LANE", raising=False)
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    assert appender_from_environment() is None
    monkeypatch.setenv("ONEX_LAB_FILL_LEDGER_BUS_LANE", "dev")
    assert appender_from_environment() is None
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    assert appender_from_environment() is not None


def test_placement_reader_maps_the_runners_pool_read(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "merge-drain/scripts/landing_placement.py").write_text(
        textwrap.dedent(
            """
            from types import SimpleNamespace as NS
            def placement_dir(): return "p"
            def limited_hosts(d): return {"h2": "until later"}
            def placed_counts(d): return {}
            def load_pool(): return ["pool"]
            def read_pool(pool, placed, lane, limited):
                host = NS(name="h1", local=False, engines=("claude",))
                return [NS(host=host, error=None, cores=8, load1=1.5, busy_cores=1.0, mem_avail_gb=31.04,
                           placed=0, lane_cap=4, lane_slots=3, lane_admission_refusal=None, describe=lambda: "h1 ok")]
            """
        )
    )
    (skills / "merge-drain/scripts/codex_placement.py").write_text(
        "def codex_hosts(env): return frozenset({'h1'})\n"
    )
    os.environ["ONEX_REMOTE_LANE_DIR"] = str(tmp_path / "lanes")
    try:
        reading = dict(LocalPlacementReader(skills).read_pool()[0])
    finally:
        del os.environ["ONEX_REMOTE_LANE_DIR"]
    assert reading == {
        "name": "h1",
        "local": False,
        "error": None,
        "cores": 8,
        "load1": 1.5,
        "busy_cores": 1.0,
        "mem_avail_gb": 31.0,
        "placed": 0,
        "lane_cap": 4,
        "runner_slots": 3,
        "refusal": None,
        "login_claude": True,
        "codex_ok": True,
        "limited": None,
        "running_lanes": [],
        "describe": "h1 ok",
    }
    assert LocalPlacementReader(skills).limited_hosts() == frozenset({"h2"})
