# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator's mode of plans-today, unchanged by the move into a node: selection, task state, owners,
ranking, delegated summaries with their fact check and budget, the stalled-plan MSGs, the schema, and the
bounded child processes. Every run resolves the operator through the fake team identity declaration."""

import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)
from omnimarket.nodes.node_plans_today_effect.models import PLANS_TODAY_JSON_SCHEMA
from omnimarket.nodes.node_plans_today_effect.runtime_plans_today import (
    main as cli_main,
)
from tests.nodes.node_plans_today_effect.conftest import (
    NOW,
    body,
    by_title,
    git,
    invoke,
    ledger,
    make_fixture,
    pr,
    row,
    write_plans,
)

pytestmark = pytest.mark.unit

HANDLER_SOURCE = Path(plans_today.__file__)


def glm_plugin(root):
    """A plugin root holding the GLM harness script, so the GLM rung is offered."""
    harness = (
        root
        / "plugin"
        / "skills"
        / "harness-delegate"
        / "scripts"
        / "harness_delegate.py"
    )
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.touch()
    return root / "plugin"


def test_selection_origin_main_and_reasons(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {
            "2026-09-20-a.md": body("A", [("first", "OMN-1001")]),
            "2026-08-01-b.md": body("B", [("second", "OMN-1002")], "\nrepo#7\n"),
        },
        origin=True,
    )
    other = tmp_path / "other"
    git("clone", fx["origin"], other)
    git("-C", other, "config", "user.name", "Test")
    git("-C", other, "config", "user.email", "test@example.invalid")
    git("-C", other, "checkout", "main")
    write_plans(other, {"2026-09-28-c.md": body("C", [("third", "OMN-1003")])})
    git("-C", other, "add", "beta/plans")
    git("-C", other, "commit", "-m", "new remote plan")
    git("-C", other, "push", "origin", "main")
    git("-C", fx["kb"], "fetch", "origin", "main")
    assert not (fx["kb"] / "beta/plans/2026-09-28-c.md").exists()
    code, doc = invoke(fx)
    assert code == 0
    assert set(by_title(doc)) == {"A", "C"}, (
        "remote C and recent A must appear; old B must not"
    )
    fx["linear"].write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "id": "OMN-1002",
                        "status": "In Progress",
                        "statusType": "started",
                        "updatedAt": "2026-09-29T15:00:00Z",
                    }
                ]
            }
        )
    )
    code, doc = invoke(fx)
    assert code == 0
    assert "in-progress-ticket" in by_title(doc)["B"]["selected_by"]
    fx["linear"].write_text('{"issues": []}')
    watcher = json.loads(fx["watcher"].read_text())
    watcher["prs"]["repo#7"] = pr("OMN-1990")
    fx["watcher"].write_text(json.dumps(watcher))
    code, doc = invoke(fx)
    assert code == 0
    assert "open-pr" in by_title(doc)["B"]["selected_by"]
    watcher["prs"] = {}
    fx["watcher"].write_text(json.dumps(watcher))
    code, doc = invoke(fx)
    assert code == 0, "old plan needs an active selection reason"
    assert "B" not in by_title(doc), "old plan needs an active selection reason"


def test_row_state_owners_idle_and_no_github(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {
            "2026-09-28-rows.md": body(
                "Rows",
                [
                    ("red", "OMN-1001"),
                    ("green", "OMN-1002"),
                    ("merged", "OMN-1003"),
                    ("idle", "OMN-1004"),
                    ("unticketed", ""),
                ],
            )
        },
        prs={
            "repo#1": pr("OMN-1001", updated="2026-09-29T14:00:00Z"),
            "repo#2": pr("OMN-1002", "green-unarmed", updated="2026-09-29T15:00:00Z"),
            "repo#3": pr("OMN-1003", "pending", "MERGED", "2026-09-29T12:00:00Z"),
        },
        rows=[
            row(1, "2026-09-29T15:50:00Z", "CLAIM", "live", "OMN-1001"),
            row(2, "2026-09-29T13:00:00Z", "CLAIM", "old", "OMN-1002"),
        ],
    )
    original_run, original_popen = subprocess.run, subprocess.Popen
    seen = []

    def check(argv):
        parts = list(argv) if not isinstance(argv, str) else argv.split()
        seen.append(parts)
        if Path(str(parts[0])).name == "gh" or any(
            str(x) == "gh" or urlparse(str(x)).hostname == "api.github.com"
            for x in parts
        ):
            raise AssertionError(f"GitHub client/API spawned: {parts}")

    def spy_run(argv, *args, **kwargs):
        check(argv)
        return original_run(argv, *args, **kwargs)

    def spy_popen(argv, *args, **kwargs):
        check(argv)
        return original_popen(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(plans_today.subprocess, "run", spy_run)
        patch.setattr(plans_today.subprocess, "Popen", spy_popen)
        with pytest.raises(AssertionError, match="GitHub"):
            spy_run(["gh", "api", "x"])
        seen.clear()
        code, doc = invoke(fx)
    assert code == 0
    assert any(Path(str(a[0])).name == "git" for a in seen)
    assert all(Path(str(a[0])).name != "gh" for a in seen)
    source = HANDLER_SOURCE.read_text()
    assert not re.search(r'\[\s*["\']gh["\']', source)
    p = by_title(doc)["Rows"]
    tasks = {t["label"]: t for t in p["tasks"]}
    assert [tasks[f"T{i}"]["state"] for i in range(1, 5)] == [
        "open-red",
        "open-green",
        "merged",
        "not-started",
    ]
    assert tasks["T5"]["no_ticket"] is True
    assert p["counts"]["no-ticket"] == 1
    assert p["counts"]["not-started"] == 1
    assert tasks["T1"]["owner"] == "live"
    assert tasks["T2"]["owner"].startswith("NONE (stale")
    assert tasks["T4"]["owner"] == "NONE"
    assert p["hours_idle"] == pytest.approx(1 / 6, abs=0.05), (
        "newest ticket row is 10 minutes old"
    )


def test_stalled_rank_order(tmp_path, monkeypatch):
    names = [
        "long",
        "short",
        "owned-stalled",
        "unowned-moving",
        "owned-moving",
        "virgin",
    ]
    plans = {
        f"2026-09-28-{name}.md": body(name, [("work", f"OMN-{1101 + i}")])
        for i, name in enumerate(names)
    }
    prs = {
        "repo#1": pr("OMN-1101", updated="2026-09-27T14:00:00Z"),
        "repo#2": pr("OMN-1102", updated="2026-09-28T00:00:00Z"),
        "repo#3": pr("OMN-1103", "green-unarmed", updated="2026-09-28T10:00:00Z"),
        "repo#4": pr("OMN-1104", "green-unarmed", updated="2026-09-29T14:00:00Z"),
        "repo#5": pr("OMN-1105", "green-unarmed", updated="2026-09-29T15:00:00Z"),
    }
    rows = [
        row(1, "2026-09-28T10:00:00Z", "CLAIM", "owned-stall", "OMN-1103"),
        row(2, "2026-09-29T15:50:00Z", "NOTE", "owned-stall", "OMN-9003"),
        row(3, "2026-09-29T15:00:00Z", "CLAIM", "owned-move", "OMN-1105"),
        row(4, "2026-09-29T15:50:00Z", "NOTE", "owned-move", "OMN-9005"),
    ]
    fx = make_fixture(tmp_path, monkeypatch, plans, prs, rows)
    code, doc = invoke(fx)
    assert code == 0
    assert [p["title"] for p in doc["plans"]] == names
    assert [p["verdict"] for p in doc["plans"]] == ["STALLED"] * 3 + [
        "IN-PROGRESS"
    ] * 2 + ["NOT-STARTED"]
    assert [p["owner"] for p in doc["plans"]] == [
        "NONE",
        "NONE",
        "owned-stall",
        "NONE",
        "owned-move",
        "NONE",
    ]
    assert doc["plans"][0]["hours_idle"] > doc["plans"][1]["hours_idle"]


def is_glm_harness(argv):
    """The GLM rung: harness_delegate.py run --harness claude-glm, never an onex delegate backend pin."""
    return (
        any(
            str(a).endswith("harness-delegate/scripts/harness_delegate.py")
            for a in argv
        )
        and "run" in argv
        and argv[argv.index("--harness") + 1] == "claude-glm"
        and "--backend-id" not in argv
    )


def test_lab_delegations_pass_caller_lane_on_both_routes(tmp_path, monkeypatch):
    """OMN-20288: the scheduler's real identity survives the no-consumer retry."""
    wrapper = tmp_path / "omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.touch()
    plan = {
        "title": "Example",
        "verdict": "STALLED",
        "owner": "NONE",
        "hours_idle": 30,
        "counts": {
            "merged": 1,
            "open-green": 0,
            "open-red": 0,
            "not-started": 0,
            "no-ticket": 0,
        },
        "tasks": [{"label": "T1"}],
    }
    calls = []

    def refused(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "no bound consumer")

    monkeypatch.setenv("ONEX_LANE", "parent-lane")
    monkeypatch.setattr(plans_today, "run", refused)
    result = plans_today.delegate_prose(
        plan, tmp_path, 1, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "template"
    assert len(calls) == 3
    assert is_glm_harness(calls[2])
    for argv, locus in zip(calls[:2], ("deployed-lane", "in-process"), strict=True):
        assert argv[argv.index("--locus") + 1] == locus
        assert argv.count("--caller-lane") == 1
        assert argv[argv.index("--caller-lane") + 1] == "plans-today"
        assert argv[argv.index("--state-root") + 1] == str(tmp_path / ".onex_state")
    assert calls[2][calls[2].index("--lane") + 1] == "plans-today"


def test_lab_delegation_asks_for_the_json_form_the_reader_parses(tmp_path, monkeypatch):
    """OMN-17427: `onex delegate` now prints the plain answer and an artifacts line unless given
    --json, so the reader found no JSON line and every 2026-10-14 summary read 'lab: exit 0, no answer'."""
    wrapper = tmp_path / "omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.touch()
    plan = {
        "title": "Example",
        "verdict": "STALLED",
        "owner": "NONE",
        "hours_idle": 30,
        "counts": {
            "merged": 1,
            "open-green": 0,
            "open-red": 0,
            "not-started": 0,
            "no-ticket": 0,
        },
        "tasks": [{"label": "T1"}],
    }
    answer = (
        "Verdict STALLED: 1 tasks, 1 merged. Owner NONE; last movement 30 hours ago."
    )
    plain = f"{answer}\ndelegate artifacts: /s/runs/r-7/result.txt /s/runs/r-7/receipt.json state_root=/s\n"
    assert plans_today.extract_response(plain) == ("", ""), (
        "the plain form is not what the reader parses"
    )
    receipt = {
        "run_id": "r-7",
        "result": {"terminal_payload": {"payload": {"response": answer}}},
    }
    calls = []

    def fake_onex(argv, **kwargs):
        calls.append(argv)
        out = json.dumps(receipt) if "--json" in argv else plain
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(plans_today, "run", fake_onex)
    result = plans_today.delegate_prose(
        plan, tmp_path, 5, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "lab"
    assert result["runs"] == ["r-7"]
    assert result["rejected"] == []
    assert calls[0].count("--json") == 1
    assert calls[0][-2:] == [
        "--locus",
        "deployed-lane",
    ]


def fake_harness(tmp_path, answer, outcome="done", code=0):
    """A harness_delegate.py stand-in: writes answer.md and receipt.json, prints the HARNESS-DELEGATE line."""
    seen = []

    def call(argv, **kwargs):
        seen.append(argv)
        prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
        assert "Write exactly two plain sentences" in prompt, (
            "the prompt goes in the prompt file, never argv"
        )
        run_dir = tmp_path / "harness-run"
        run_dir.mkdir(exist_ok=True)
        (run_dir / "answer.md").write_text(answer, encoding="utf-8")
        receipt = {
            "run_id": "h-1",
            "outcome": outcome,
            "answer_file": str(run_dir / "answer.md"),
            "backend_id": "harness:claude-glm:glm-5.3-flash",
            "model": "glm-5.3-flash",
        }
        (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        line = f"HARNESS-DELEGATE harness=claude-glm model=glm-5.3-flash outcome={outcome} receipt={run_dir / 'receipt.json'}"
        return subprocess.CompletedProcess(argv, code, "noise\n" + line + "\n", "")

    return seen, call


def test_glm_rung_runs_through_claude_code_and_its_answer_is_fact_checked(
    tmp_path, monkeypatch
):
    """OMN-20173: the second rung is GLM through harness-delegate claude-glm; its answer.md is the summary."""
    plan = {
        "title": "Example",
        "verdict": "STALLED",
        "owner": "NONE",
        "hours_idle": 30,
        "counts": {
            "merged": 1,
            "open-green": 1,
            "open-red": 1,
            "not-started": 1,
            "no-ticket": 1,
        },
        "tasks": [{"label": f"T{i}"} for i in range(5)],
    }
    wrapper = tmp_path / "omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.touch()
    good = "Verdict STALLED: 5 tasks, 1 merged, 1 open-green, 1 open-red, 1 not-started. Owner NONE."
    seen, harness = fake_harness(tmp_path, good)
    lab_calls = []

    def route(argv, **kwargs):
        if is_glm_harness(argv):
            return harness(argv, **kwargs)
        lab_calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "lab refused")

    monkeypatch.setattr(plans_today, "run", route)
    result = plans_today.delegate_prose(
        plan, tmp_path, 5, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "glm"
    assert result["summary"] == good
    assert result["runs"] == ["h-1"]
    assert len(lab_calls) == 1
    assert len(seen) == 1
    argv = seen[0]
    assert argv[argv.index("--model") + 1] == "glm-5.3-flash"
    assert "--check" in argv
    assert "--writable-repo" not in argv
    assert not Path(argv[argv.index("--prompt-file") + 1]).exists(), (
        "the prompt file is removed after the run"
    )
    # a GLM answer that fails the fact check falls back to the template
    _seen2, bad_harness = fake_harness(tmp_path, "Verdict STALLED: 9 tasks.")
    monkeypatch.setattr(
        plans_today,
        "run",
        lambda argv, **_kw: (
            bad_harness(argv)
            if is_glm_harness(argv)
            else subprocess.CompletedProcess(argv, 1, "", "")
        ),
    )
    result = plans_today.delegate_prose(
        plan, tmp_path, 5, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "template"
    assert any(r.startswith("glm: rejected") for r in result["rejected"])
    # a harness that did not finish (not logged in, timed out) is a refusal, not an answer
    _seen3, failed = fake_harness(
        tmp_path, "Verdict STALLED: 5 tasks.", outcome="not-logged-in", code=77
    )
    monkeypatch.setattr(
        plans_today,
        "run",
        lambda argv, **_kw: (
            failed(argv)
            if is_glm_harness(argv)
            else subprocess.CompletedProcess(argv, 1, "", "")
        ),
    )
    result = plans_today.delegate_prose(
        plan, tmp_path, 5, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "template"
    assert any(r.startswith("glm: exit 77") for r in result["rejected"])


def test_factcheck_extract_and_delegate_fallback(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-basic.md": body("Basic", [("work", "OMN-1001")])},
    )
    assert invoke(fx)[0] == 0
    plan = {
        "title": "Example",
        "verdict": "STALLED",
        "owner": "NONE",
        "hours_idle": 30,
        "counts": {
            "merged": 1,
            "open-green": 1,
            "open-red": 1,
            "not-started": 1,
            "no-ticket": 1,
        },
        "tasks": [{"label": f"T{i}"} for i in range(5)],
    }
    faithful = "Verdict STALLED: 5 tasks, 1 merged, 1 open-green, 1 open-red, 1 not-started. Owner NONE; last movement 30 hours ago."
    assert plans_today.factcheck(faithful, plan) == []
    assert any(
        "7" in finding
        for finding in plans_today.factcheck(
            faithful.replace("5 tasks", "7 tasks"), plan
        )
    )
    assert any(
        "OMN-9999" in finding
        for finding in plans_today.factcheck(faithful + " OMN-9999", plan)
    )
    assert any(
        "verdict" in finding
        for finding in plans_today.factcheck(faithful.replace("STALLED", "quiet"), plan)
    )
    assert any(
        "open-red" in f
        for f in plans_today.factcheck(
            faithful.replace("1 open-red", "3 open-red"), plan
        )
    ), "a count on the wrong label"
    assert (
        plans_today.factcheck(
            "STALLED, 5 tasks: 1 is not-started and 1 is open-red, 30 hours idle.", plan
        )
        == []
    )
    assert any(
        "hours" in f
        for f in plans_today.factcheck(faithful.replace("30 hours", "12 hours"), plan)
    )
    fine = "STALLED, 5 tasks in all: 1 merged, 1 open-green, 1 open-red, 1 not-started, and 1 task with no ticket, 30 hours."
    assert plans_today.factcheck(fine, plan) == [], (
        "a 'with no ticket' count is not read as a task total"
    )
    assert (
        plans_today.factcheck(
            "STALLED, 5 tasks of which 1 is merged; 1 tasks have no ticket.", plan
        )
        == []
    )
    assert any(
        "no ticket" in f
        for f in plans_today.factcheck(faithful + " 4 tasks have no ticket.", plan)
    )
    payload = '{"result":{"terminal_payload":{"payload":{"response":"Two sentences."}}},"run_id":"r-1"}\n'
    assert plans_today.extract_response(payload) == ("Two sentences.", "r-1")
    wrapper = tmp_path / "omnibase_infra/scripts/onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.touch()
    calls = []

    def rejected(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "result": {
                        "terminal_payload": {
                            "payload": {"response": "Verdict STALLED: 7 tasks."}
                        }
                    },
                    "run_id": "r-1",
                }
            ),
            "",
        )

    monkeypatch.setattr(plans_today, "run", rejected)
    result = plans_today.delegate_prose(
        plan, tmp_path, 1, plugin_root=glm_plugin(tmp_path)
    )
    assert result["summary_source"] == "template"
    assert len(calls) == 2, (
        "a rejected lab summary goes to GLM next; the in-process retry is for a lane with no consumer"
    )
    assert calls[0][-2:] == ["--locus", "deployed-lane"]
    assert is_glm_harness(calls[1]), (
        "GLM runs only through Claude Code (harness-delegate claude-glm), OMN-20173"
    )
    assert not any("cloud-glm" in a for c in calls for a in c), (
        "the cloud-glm backend pin is retired (omnimarket#3136)"
    )
    assert any("rejected" in r for r in result["rejected"])
    # a lane with no bound consumer gets the in-process retry once, then GLM
    calls.clear()
    monkeypatch.setattr(
        plans_today,
        "run",
        lambda argv, **_kw: (
            calls.append(argv),
            subprocess.CompletedProcess(argv, 1, "", "no bound consumer"),
        )[1],
    )
    assert (
        plans_today.delegate_prose(plan, tmp_path, 1, plugin_root=glm_plugin(tmp_path))[
            "summary_source"
        ]
        == "template"
    )
    assert [c[-2:] for c in calls[:2]] == [
        ["--locus", "deployed-lane"],
        ["--locus", "in-process"],
    ]
    assert is_glm_harness(calls[2])
    assert not any("cloud-glm" in a for c in calls for a in c)
    # a faithful lab summary is used, and a rerun with the same facts reads the cache instead of delegating
    good = "Verdict STALLED: 5 tasks, 1 merged, 1 open-green, 1 open-red, 1 not-started. Owner NONE."
    ok = json.dumps(
        {
            "result": {"terminal_payload": {"payload": {"response": good}}},
            "run_id": "r-9",
        }
    )
    calls.clear()
    monkeypatch.setattr(
        plans_today,
        "run",
        lambda argv, **_kw: (
            calls.append(argv),
            subprocess.CompletedProcess(argv, 0, ok, ""),
        )[1],
    )
    plan["counts"]["no-ticket"] = 1
    cache = tmp_path / "cache.json"
    plans_today.add_summaries(
        [plan], "top", 8, tmp_path, 1, 1, cache, plugin_root=glm_plugin(tmp_path)
    )
    assert plan["summary_source"] == "lab"
    assert plan["delegation_runs"] == ["r-9"]
    assert len(calls) == 1
    plans_today.add_summaries(
        [plan], "top", 8, tmp_path, 1, 1, cache, plugin_root=glm_plugin(tmp_path)
    )
    assert plan["summary_source"] == "lab", "unchanged facts must not delegate again"
    assert len(calls) == 1, "unchanged facts must not delegate again"


def test_stalled_msg_dedupe_retry_and_text(tmp_path, monkeypatch):
    plans = {
        "2026-09-28-a.md": body("A", [("work", "OMN-1201")]),
        "2026-09-28-b.md": body("B", [("work", "OMN-1202")]),
        "2026-09-28-owned.md": body("Owned", [("work", "OMN-1203")]),
    }
    prs = {
        "repo#1": pr("OMN-1201", updated="2026-09-28T10:00:00Z"),
        "repo#2": pr("OMN-1202", updated="2026-09-28T11:00:00Z"),
        "repo#3": pr("OMN-1203", "green-unarmed", updated="2026-09-28T10:00:00Z"),
    }
    rows = [
        row(1, "2026-09-28T10:00:00Z", "CLAIM", "live", "OMN-1203"),
        row(2, "2026-09-29T15:50:00Z", "NOTE", "live", "OMN-9000"),
    ]
    fx = make_fixture(tmp_path, monkeypatch, plans, prs, rows)
    calls = []

    def send(plan, to, lane, home, plugin_root=None):
        calls.append(plan)
        return True, "OK MSG x"

    monkeypatch.setattr(plans_today, "emit_msg", send)
    assert invoke(fx)[0] == 0
    assert calls == []
    code, doc = invoke(fx, no_msg=False)
    assert code == 0
    assert {p["title"] for p in calls} == {"A", "B"}
    for p in calls:
        text = plans_today.msg_text(p)
        assert p["path"] in text
        assert p["next_action"] in text
        assert "|" not in text
    calls.clear()
    code, doc = invoke(fx, no_msg=False)
    assert code == 0
    assert calls == []
    assert all(m["action"] == "skipped" for m in doc["msgs"])
    later = "2026-09-30T05:00:00Z"
    watcher = json.loads(fx["watcher"].read_text())
    watcher["last_tick"] = "2026-09-30T04:55:00Z"
    fx["watcher"].write_text(json.dumps(watcher))
    fx["ledger"].write_text(
        ledger([*rows, row(3, "2026-09-30T04:50:00Z", "NOTE", "live", "OMN-9000")])
    )
    code, doc = invoke(fx, now=later, no_msg=False)
    assert code == 0
    assert {p["title"] for p in calls} == {"A", "B"}
    calls.clear()
    assert invoke(fx, now=later)[0] == 0
    assert calls == []
    failed_out = tmp_path / "failed-out"
    monkeypatch.setattr(plans_today, "emit_msg", lambda *_args: (False, "REFUSED x"))
    code, doc = invoke(fx, now=later, no_msg=False, out=failed_out)
    assert code == 4
    assert all(m["action"] == "failed" for m in doc["msgs"])
    assert not (failed_out / "msg-dedupe.json").exists()
    monkeypatch.setattr(plans_today, "emit_msg", send)
    code, doc = invoke(fx, now=later, no_msg=False, out=failed_out)
    assert code == 0
    assert {p["title"] for p in calls} == {"A", "B"}


def test_schema_validation_and_contract(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path, monkeypatch, {"2026-09-28-a.md": body("A", [("work", "OMN-1001")])}
    )
    code, doc = invoke(fx)
    assert code == 0
    assert plans_today.validate(doc) == []
    doc["plans"][0]["verdict"] = "BOGUS"
    assert plans_today.validate(doc)
    doc["plans"][0]["verdict"] = "NOT-STARTED"
    doc["plans"][0]["tasks"][0]["state"] = "started"
    assert plans_today.validate(doc)
    schema = PLANS_TODAY_JSON_SCHEMA
    assert schema["$id"] == "omni.plans-today/2"
    assert schema["required"] == [
        "schema",
        "generated_at",
        "mode",
        "scope",
        "sources",
        "plans",
        "msgs",
    ]


def test_schema_goal_state_as_of_is_read_from_origin_main(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-a.md": body("A", [("work", "OMN-1001")])},
        origin=True,
    )
    goal = fx["kb"] / "beta" / "GOAL.md"
    goal.write_text("state_as_of: 2026-09-29\n\nthe week\n")
    git("-C", fx["kb"], "add", "beta/GOAL.md")
    git("-C", fx["kb"], "commit", "-m", "goal")
    git("-C", fx["kb"], "push", "origin", "main")
    git("-C", fx["kb"], "fetch", "origin", "main")
    code, doc = invoke(fx)
    assert code == 0
    assert doc["sources"]["goal_state_as_of"] == "2026-09-29"
    goal.write_text("state_as_of: 1999-01-01\n")  # a working-tree edit is never read
    assert invoke(fx)[1]["sources"]["goal_state_as_of"] == "2026-09-29"


def test_no_linear_refused(tmp_path, monkeypatch, capsys):
    fx = make_fixture(
        tmp_path, monkeypatch, {"2026-09-28-a.md": body("A", [("work", "OMN-1001")])}
    )
    code, _ = invoke(fx, linear=False)
    assert code == 2
    assert "REFUSED" in capsys.readouterr().err
    # --no-linear leaves no sidecar to resolve the operator from: team restrictions, never the operator's run
    assert invoke(fx, linear="disabled")[0] == 2


def test_stale_watcher_refused(tmp_path, monkeypatch, capsys):
    fx = make_fixture(
        tmp_path, monkeypatch, {"2026-09-28-a.md": body("A", [("work", "OMN-1001")])}
    )
    watcher = json.loads(fx["watcher"].read_text())
    watcher["last_tick"] = "2026-09-29T13:00:00Z"
    fx["watcher"].write_text(json.dumps(watcher))
    code, _ = invoke(fx)
    assert code == 2
    assert "REFUSED" in capsys.readouterr().err
    watcher["last_tick"] = "2026-09-29T15:55:00Z"
    fx["watcher"].write_text(json.dumps(watcher))
    assert invoke(fx)[0] == 0


def test_row_state_linear_compact_snapshot_marks_active_not_started(
    tmp_path, monkeypatch
):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-lin.md": body("Lin", [("a", "OMN-1001"), ("b", "OMN-1002")])},
    )
    compact = tmp_path / "linear.txt"
    compact.write_text(
        "OMN-1001 In Progress 2026-09-29T15:00:00Z\nnot a row\nOMN-1002 Todo 2026-09-20T15:00:00Z\n"
    )
    fx["linear"] = compact
    code, doc = invoke(fx)
    plan = by_title(doc)["Lin"]
    tasks = {t["label"]: t for t in plan["tasks"]}
    assert code == 0
    assert tasks["T1"]["state"] == "not-started"
    assert tasks["T1"]["active"] is True
    assert tasks["T2"]["active"] is False
    assert plan["counts"]["not-started-active"] == 1
    assert plan["next_action"].startswith("dispatch a lane for T2"), (
        "the next task is one nobody is touching"
    )
    loaded = plans_today.load_linear(compact)
    assert set(loaded) == {"OMN-1001", "OMN-1002"}
    assert loaded["OMN-1001"].status_type == "started"


def test_selection_plan_filter_limits_output_and_msgs(tmp_path, monkeypatch):
    plans = {
        "2026-09-28-a.md": body("A", [("w", "OMN-1201")]),
        "2026-09-28-b.md": body("B", [("w", "OMN-1202")]),
    }
    prs = {
        "repo#1": pr("OMN-1201", updated="2026-09-28T10:00:00Z"),
        "repo#2": pr("OMN-1202", updated="2026-09-28T11:00:00Z"),
    }
    fx = make_fixture(tmp_path, monkeypatch, plans, prs)
    sent = []
    monkeypatch.setattr(
        plans_today,
        "emit_msg",
        lambda p, *_a: (sent.append(p["title"]), (True, "OK MSG x"))[1],
    )
    argv = [
        "--no-fetch",
        "--summaries",
        "none",
        "--watcher",
        str(fx["watcher"]),
        "--ledger-rows-file",
        str(fx["ledger"]),
        "--kb",
        str(fx["kb"]),
        "--out-dir",
        str(fx["out"]),
        "--now",
        NOW,
        "--linear-snapshot",
        str(fx["linear"]),
        "--plan",
        "28-b.md",
    ]
    assert cli_main(argv) == 0
    assert sent == ["B"]
    assert [
        p["title"]
        for p in json.loads((fx["out"] / "plans-today.json").read_text())["plans"]
    ] == ["B"]


def test_row_state_plan_without_a_task_table_says_what_to_do(tmp_path, monkeypatch):
    text = "# Prose plan\n\nTicket: OMN-1500\n\nJust words, no table.\n"
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-prose.md": text},
        rows=[row(1, "2026-09-20T10:00:00Z", "CLAIM", "old", "OMN-1500")],
    )
    code, doc = invoke(fx)
    plan = by_title(doc)["Prose plan"]
    assert code == 0
    assert plan["tasks"] == []
    assert plan["next_action"].startswith(
        "read the plan: no task table maps its ticket (OMN-1500)"
    )


# ------------------------------------------------------------------ the 2026-10-09 hang (OMN-20787)
#
# FRICTION 2026-10-09T07:28:43Z blamed a pipe-buffer deadlock. subprocess.run(input=..., capture_output=True)
# cannot deadlock on buffer size (communicate() multiplexes), so that cause is not assumed: the first test
# below is its positive control. The measured causes are the ones the other tests reproduce: the summary
# phase stacked a lab call (up to 240 s, 179 s measured against a down lane), an in-process retry (240 s)
# and a GLM call (240 s + 60 s) per plan with no total bound, so a default run outlasts a ten-minute caller;
# the GLM rung asked the harness for a timeout it refuses; and every other child ran with no deadline, with
# the caller's stdin, on pipes a grandchild can hold open.


def _sleeper(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nsleep {seconds}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _pid_gone(pid, wait=3.0):
    import os
    import time

    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def test_hang_reproduction_pipe_buffer_is_not_the_cause():
    """Positive control for FRICTION 07:28:43Z: 4 MB in and 4 MB out through the helper returns at once."""
    import time

    blob = "x" * (4 * 1024 * 1024)
    started = time.monotonic()
    r = plans_today.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input=blob,
        timeout=30,
    )
    assert r.returncode == 0
    assert r.stdout == blob
    assert time.monotonic() - started < 15


def test_hang_reproduction_summary_phase_is_bounded_by_a_total_budget(
    tmp_path, monkeypatch
):
    """A lab call and a GLM call that each outlast the budget leave the plan on its template, inside the budget."""
    import time

    _sleeper(tmp_path / "omnibase_infra/scripts/onex", 6)
    plugin = glm_plugin(tmp_path)
    fake_harness_script = plugin.joinpath(*plans_today.HARNESS_DELEGATE_RELPATH)
    fake_harness_script.write_text("import time\ntime.sleep(6)\n", encoding="utf-8")
    monkeypatch.setattr(plans_today, "SUMMARY_BUDGET_S", 2, raising=False)
    plan = {
        "title": "Example",
        "verdict": "STALLED",
        "owner": "NONE",
        "hours_idle": 30,
        "counts": {
            "merged": 1,
            "open-green": 0,
            "open-red": 0,
            "not-started": 0,
            "no-ticket": 0,
        },
        "tasks": [{"label": "T1"}],
    }
    started = time.monotonic()
    plans_today.add_summaries(
        [plan], "top", 8, tmp_path, 240, 1, tmp_path / "cache.json", plugin_root=plugin
    )
    elapsed = time.monotonic() - started
    assert plan["summary_source"] == "template"
    assert elapsed < 5, f"summary phase took {elapsed:.1f}s against a 2 s budget"
    assert any("budget" in r or "timed out" in r for r in plan["summary_rejections"])


def test_hang_reproduction_grandchild_holding_the_pipe_does_not_block_run(tmp_path):
    """A child that exits while its own child keeps stdout open used to hold run() until the grandchild ended."""
    import os
    import signal
    import time

    pidfile = tmp_path / "grandchild.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys\n"
        f"p = subprocess.Popen(['sleep', '8'], start_new_session=True)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "print('child done', flush=True)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    try:
        r = plans_today.run([sys.executable, str(child)], timeout=30)
        elapsed = time.monotonic() - started
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
    assert r.returncode == 0
    assert r.stdout.strip() == "child done"
    assert elapsed < 4, (
        f"run() waited {elapsed:.1f}s for a grandchild that held the pipe"
    )


def test_hang_reproduction_child_does_not_inherit_stdin():
    """A child that reads stdin blocked forever when the caller's stdin was an open pipe."""
    code = (
        "from omnimarket.nodes.node_plans_today_effect.handlers import handler_plans_today as plans_today; "
        "import sys; "
        "r = plans_today.run([sys.executable, '-c', 'import sys; sys.stdin.read(); print(1)'], timeout=60); "
        "print('inner rc', r.returncode)"
    )
    r_fd, w_fd = __import__("os").pipe()
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            stdin=r_fd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        __import__("os").close(w_fd)
        __import__("os").close(r_fd)
    assert "inner rc 0" in out.stdout, out.stderr


def test_hang_reproduction_every_child_has_a_deadline(monkeypatch):
    """No child runs unbounded: without a timeout argument the default deadline applies."""
    import time

    monkeypatch.setattr(plans_today, "CHILD_TIMEOUT_S", 1, raising=False)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        plans_today.run([sys.executable, "-c", "import time; time.sleep(8)"])
    assert time.monotonic() - started < 5


def test_hang_reproduction_timeout_kills_the_whole_process_group(tmp_path):
    import os
    import signal

    pidfile = tmp_path / "grandchild.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '30'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            plans_today.run([sys.executable, str(child)], timeout=1.5)
        assert pidfile.exists()
        assert _pid_gone(int(pidfile.read_text())), (
            "the grandchild outlived the timeout"
        )
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_hang_reproduction_a_stuck_fetch_refuses_instead_of_hanging(
    tmp_path, monkeypatch
):
    import time

    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-a.md": body("A", [("work", "OMN-1001")])},
        origin=True,
    )
    git("-C", fx["kb"], "remote", "set-url", "origin", "ssh://stuck.invalid/kb.git")
    slow_ssh = tmp_path / "slow_ssh.sh"
    slow_ssh.write_text("#!/bin/sh\nsleep 8\n", encoding="utf-8")
    slow_ssh.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(slow_ssh))
    monkeypatch.setattr(plans_today, "CHILD_TIMEOUT_S", 1, raising=False)
    started = time.monotonic()
    with pytest.raises(plans_today.RefusedError, match="timed out"):
        plans_today.kb_revision(fx["kb"], fetch=True)
    assert time.monotonic() - started < 5


def test_hang_reproduction_glm_timeout_fits_the_harness_budget(tmp_path):
    """harness_delegate refuses --timeout-s of 175 s or more; the GLM rung never asks for more than 170 s."""
    harness = tmp_path / "h.py"
    argv = plans_today.glm_argv(
        tmp_path / "p.md", tmp_path, 240, harness
    )  # the default --summary-timeout
    assert (
        int(argv[argv.index("--timeout-s") + 1]) == plans_today.GLM_TIMEOUT_MAX_S == 170
    )
    argv = plans_today.glm_argv(tmp_path / "p.md", tmp_path, 9999, harness)
    assert int(argv[argv.index("--timeout-s") + 1]) <= 170
    assert argv[1] == str(harness)


# ------------------------------------------------------------------ --json: stalled plans (OMN-20787)


def _schema_findings(value, schema, path="$"):
    """The keywords plans_today.schema.json uses: type, enum, const, pattern, minimum, required, properties,
    items, additionalProperties (false), allOf, if and then. The repository's CI has no jsonschema package."""
    found = []
    if "allOf" in schema:
        for sub in schema["allOf"]:
            found += _schema_findings(value, sub, path)
    if "if" in schema and not _schema_findings(value, schema["if"], path):
        found += _schema_findings(value, schema.get("then", {}), path)
    if "const" in schema and value != schema["const"]:
        found.append(f"{path}: not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: {value!r} not in {schema['enum']}")
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        py = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }
        if not any(
            isinstance(value, py[k])
            and not (k in ("integer", "number") and isinstance(value, bool))
            for k in kinds
        ):
            return [*found, f"{path}: {type(value).__name__} is not {kinds}"]
    if (
        isinstance(value, str)
        and "pattern" in schema
        and not re.search(schema["pattern"], value)
    ):
        found.append(f"{path}: does not match {schema['pattern']}")
    if (
        isinstance(value, (int, float))
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        found.append(f"{path}: below {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                found.append(f"{path}: lacks {key}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                found += _schema_findings(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            found += [f"{path}: unexpected {k}" for k in value if k not in props]
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            found += _schema_findings(item, schema["items"], f"{path}[{i}]")
    return found


def _json_stalled_fixture(tmp_path, monkeypatch):
    plans = {
        "2026-09-28-header.md": "# Header plan\n\n**Priority:** High\n\n## Tasks\n\n| Task | Ticket |\n| -- | -- |\n| T1 red | OMN-2001 |\n| T2 later | OMN-2002 |\n",
        "2026-09-28-linear.md": body("Linear plan", [("todo", "OMN-2101")]),
        "2026-09-28-none.md": body("No priority", [("todo", "OMN-2201")]),
        "2026-09-28-green.md": body("Green plan", [("green", "OMN-2301")]),
    }
    prs = {
        "repo#1": pr("OMN-2001", updated="2026-09-28T10:00:00Z"),
        "repo#4": pr("OMN-2301", "green-unarmed", updated="2026-09-28T10:00:00Z"),
    }
    rows = [
        row(1, "2026-09-28T09:00:00Z", "CLAIM", "old-a", "OMN-2101"),
        row(2, "2026-09-28T09:00:00Z", "CLAIM", "old-b", "OMN-2201"),
    ]
    fx = make_fixture(tmp_path, monkeypatch, plans, prs=prs, rows=rows)
    fx["linear"].write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "id": "OMN-2101",
                        "status": "Todo",
                        "statusType": "unstarted",
                        "updatedAt": "2026-09-28T09:00:00Z",
                        "priority": "Urgent",
                    },
                    {
                        "id": "OMN-2201",
                        "status": "Todo",
                        "statusType": "unstarted",
                        "updatedAt": "2026-09-28T09:00:00Z",
                    },
                ]
            }
        )
    )
    return fx


def test_json_stalled_schema(tmp_path, monkeypatch, capsys):
    """--json prints the document, it validates against the schema file, and each STALLED plan carries its
    priority and its next-action ticket."""
    fx = _json_stalled_fixture(tmp_path, monkeypatch)
    argv = [
        "--no-fetch",
        "--summaries",
        "none",
        "--watcher",
        str(fx["watcher"]),
        "--ledger-rows-file",
        str(fx["ledger"]),
        "--kb",
        str(fx["kb"]),
        "--out-dir",
        str(fx["out"]),
        "--now",
        NOW,
        "--no-msg",
        "--linear-snapshot",
        str(fx["linear"]),
        "--json",
    ]
    assert cli_main(argv) == 0
    doc = json.loads(capsys.readouterr().out)
    schema = PLANS_TODAY_JSON_SCHEMA
    assert _schema_findings(doc, schema) == []
    assert plans_today.validate(doc) == []
    plans = by_title(doc)
    stalled = [p for p in doc["plans"] if p["verdict"] == "STALLED"]
    assert {p["title"] for p in stalled} == {
        "Header plan",
        "Linear plan",
        "No priority",
        "Green plan",
    }
    for p in stalled:
        assert p["priority"] in ("urgent", "high", "medium", "low", "unset")
        assert p["priority_source"] in ("plan-header", "linear", "none")
        assert "next_action_ticket" in p
        assert "next_action_kind" in p
    assert (
        plans["Header plan"]["priority"],
        plans["Header plan"]["priority_source"],
    ) == ("high", "plan-header")
    assert (
        plans["Linear plan"]["priority"],
        plans["Linear plan"]["priority_source"],
    ) == ("urgent", "linear")
    assert (
        plans["No priority"]["priority"],
        plans["No priority"]["priority_source"],
    ) == ("unset", "none")
    assert plans["Header plan"]["next_action_ticket"] == "OMN-2001"
    assert plans["Header plan"]["next_action_kind"] == "dispatch-fix"
    assert plans["Linear plan"]["next_action_ticket"] == "OMN-2101"
    assert plans["Linear plan"]["next_action_kind"] == "dispatch"
    assert plans["Green plan"]["next_action_ticket"] == "OMN-2301"
    assert plans["Green plan"]["next_action_kind"] == "hand-to-drain"


def test_json_stalled_schema_refuses_a_stalled_plan_without_priority_or_ticket(
    tmp_path, monkeypatch
):
    fx = _json_stalled_fixture(tmp_path, monkeypatch)
    code, doc = invoke(fx)
    assert code == 0
    assert plans_today.validate(doc) == []
    schema = PLANS_TODAY_JSON_SCHEMA
    stalled = next(p for p in doc["plans"] if p["verdict"] == "STALLED")
    for key in (
        "priority",
        "priority_source",
        "next_action_ticket",
        "next_action_kind",
    ):
        broken = json.loads(json.dumps(doc))
        target = next(p for p in broken["plans"] if p["path"] == stalled["path"])
        del target[key]
        assert plans_today.validate(broken), (
            f"validate() accepted a STALLED plan without {key}"
        )
        assert _schema_findings(broken, schema), (
            f"the schema accepted a STALLED plan without {key}"
        )
    broken = json.loads(json.dumps(doc))
    next(p for p in broken["plans"] if p["path"] == stalled["path"])["priority"] = (
        "critical"
    )
    assert plans_today.validate(broken)
    assert _schema_findings(broken, schema)


def test_json_stalled_next_action_ticket_for_a_plan_without_a_task_table(
    tmp_path, monkeypatch
):
    text = "# Prose plan\n\nTicket: OMN-1500\n\nJust words, no table.\n"
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-prose.md": text},
        rows=[row(1, "2026-09-20T10:00:00Z", "CLAIM", "old", "OMN-1500")],
    )
    code, doc = invoke(fx)
    plan = by_title(doc)["Prose plan"]
    assert code == 0
    assert plan["verdict"] == "STALLED"
    assert plan["next_action_ticket"] == "OMN-1500"
    assert plan["next_action_kind"] == "read-plan"
    assert plans_today.validate(doc) == []


def test_hang_reproduction_one_timed_out_send_stops_the_rest(tmp_path, monkeypatch):
    """Forty sends that each wait out a stuck ledger path are forty deadlines: after one, the rest are not tried."""
    plans = [
        {
            "path": f"beta/plans/p{i}.md",
            "title": f"P{i}",
            "verdict": "STALLED",
            "owner": "NONE",
            "verdict_reason": "r",
            "next_action": "n",
        }
        for i in range(3)
    ]
    calls = []

    def stuck(p, *args):
        calls.append(p["path"])
        return False, f"{plans_today.SEND_TIMED_OUT} after 120 s"

    monkeypatch.setattr(plans_today, "emit_msg", stuck)
    results, ok = plans_today.act(
        plans,
        tmp_path,
        plans_today.parse_ts(NOW),
        "orch",
        "plans-today",
        tmp_path,
        no_msg=False,
    )
    assert calls == ["beta/plans/p0.md"]
    assert not ok
    assert [r["action"] for r in results] == ["failed", "failed", "failed"]
    assert "earlier send timed out" in results[1]["detail"]


def test_json_stalled_priority_reads_the_compact_linear_snapshot(tmp_path, monkeypatch):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-a.md": body("A", [("work", "OMN-2101")])},
        rows=[row(1, "2026-09-28T09:00:00Z", "CLAIM", "old", "OMN-2101")],
    )
    snapshot = tmp_path / "linear.txt"
    snapshot.write_text(
        "OMN-2101 Todo 2026-09-28T09:00:00Z priority=High\nOMN-9999 In Progress 2026-09-29T09:00:00Z\n"
    )
    fx["linear"] = snapshot
    code, doc = invoke(fx)
    plan = by_title(doc)["A"]
    assert code == 0
    assert (plan["priority"], plan["priority_source"]) == (
        "high",
        "linear",
    )
    assert plans_today.load_linear(snapshot)["OMN-9999"].priority is None


# ----------------------------------------------------------------------------- failed sends (OMN-20099)

DUPLICATE_ID_REFUSAL = (
    "REFUSED 65 grammar: OMN-19256 row grammar rejected append to ROLLING_WORK_LEDGER.md -- line 1: "
    "id=2026-10-15T04:16:16Z-plans-today is already declared on this ledger; an id names one row. Nothing was written."
)
STALLED_PLAN = {
    "path": "beta/plans/p.md",
    "title": "P",
    "verdict": "STALLED",
    "owner": "NONE",
    "verdict_reason": "r",
    "next_action": "n",
}


def fake_executor(monkeypatch, sends):
    """Replace the child runner: `newdir` answers a directory, each send pops the next (rc, stdout, stderr)."""
    calls = []

    def fake_run(argv, **kw):
        if "newdir" in argv:
            return subprocess.CompletedProcess(argv, 0, "/tmp/fake-dir\n", "")
        calls.append(argv)
        rc, out, err = sends.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(plans_today, "run", fake_run)
    monkeypatch.setattr(
        plans_today.Path, "write_text", lambda _self, text, **_kw: len(text)
    )
    waits = []
    monkeypatch.setattr(plans_today.time, "sleep", waits.append)
    return calls, waits


def test_failed_send_names_the_exit_code_and_the_reason(tmp_path, monkeypatch):
    calls, waits = fake_executor(
        monkeypatch, [(75, "", "lock busy\nonex-ledger: lock timeout after 30 s\n")]
    )
    ok, detail = plans_today.emit_msg(
        STALLED_PLAN, "orch", "plans-today", tmp_path, tmp_path
    )
    assert not ok
    assert detail == "rc=75 onex-ledger: lock timeout after 30 s"
    assert len(calls) == 1
    assert waits == []


def test_refusal_reason_is_read_from_stdout_when_stderr_is_empty(tmp_path, monkeypatch):
    fake_executor(
        monkeypatch,
        [(65, "REFUSED 65 grammar: no to= cell\nFIX: add the named field\n", "")],
    )
    ok, detail = plans_today.emit_msg(
        STALLED_PLAN, "orch", "plans-today", tmp_path, tmp_path
    )
    assert not ok
    assert detail == "rc=65 REFUSED 65 grammar: no to= cell"


def test_duplicate_id_refusal_waits_out_the_second_and_sends_once_more(
    tmp_path, monkeypatch
):
    """Reproduction: the row id is <second>-<lane>, so a second MSG in the same second is refused as a duplicate."""
    calls, waits = fake_executor(
        monkeypatch,
        [
            (65, DUPLICATE_ID_REFUSAL + "\n", ""),
            (0, "OK MSG 2026-10-15T04:16:17Z from=plans-today\n", ""),
        ],
    )
    ok, detail = plans_today.emit_msg(
        STALLED_PLAN, "orch", "plans-today", tmp_path, tmp_path
    )
    assert ok
    assert detail.startswith("OK MSG")
    assert len(calls) == 2
    assert len(waits) == 1
    assert waits[0] > 1.0


def test_duplicate_id_refusal_is_retried_once_only(tmp_path, monkeypatch):
    calls, _waits = fake_executor(
        monkeypatch,
        [(65, DUPLICATE_ID_REFUSAL + "\n", ""), (65, DUPLICATE_ID_REFUSAL + "\n", "")],
    )
    ok, detail = plans_today.emit_msg(
        STALLED_PLAN, "orch", "plans-today", tmp_path, tmp_path
    )
    assert not ok
    assert detail.startswith("rc=65 REFUSED 65 grammar")
    assert len(calls) == 2


def test_other_refusals_are_not_retried(tmp_path, monkeypatch):
    calls, waits = fake_executor(
        monkeypatch, [(65, "REFUSED 65 lint: text carries a pipe\n", "")]
    )
    ok, _detail = plans_today.emit_msg(
        STALLED_PLAN, "orch", "plans-today", tmp_path, tmp_path
    )
    assert not ok
    assert len(calls) == 1
    assert waits == []


def test_failed_send_is_printed_and_recorded_in_the_detail_file(
    tmp_path, monkeypatch, capsys
):
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        {"2026-09-28-a.md": body("A", [("work", "OMN-1201")])},
        {"repo#1": pr("OMN-1201", updated="2026-09-28T10:00:00Z")},
    )
    monkeypatch.setattr(
        plans_today,
        "emit_msg",
        lambda *_args: (False, "rc=65 REFUSED 65 grammar: duplicate"),
    )
    code, _doc = invoke(fx, no_msg=False)
    out = capsys.readouterr().out
    assert code == 4
    assert (
        "MSG failed: beta/plans/2026-09-28-a.md rc=65 REFUSED 65 grammar: duplicate"
        in out
    )
    assert (
        "- failed: beta/plans/2026-09-28-a.md (rc=65 REFUSED 65 grammar: duplicate)"
        in (fx["out"] / "detail.md").read_text()
    )
