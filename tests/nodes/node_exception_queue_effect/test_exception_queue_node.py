"""The exception queue and second-occurrence-becomes-code, in the node queue_status.py calls (OMN-17427).

Operator 2026-10-18 ~22:30Z: "need to make the process as mechanical as possible, where you only have to rule on the
exceptions instead of the rule." (5) Every detector and actor resolves its case in code or emits one typed exception
row (class, evidence, what it tried, proposed fix); the operator rules only on rows marked decision. (6) A second
exception of the same class makes the queue dispatch a lane to mechanize that class.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE = "omnimarket.nodes.node_exception_queue_effect"
handler = importlib.import_module(f"{NODE}.handler")
models = importlib.import_module(f"{NODE}.models")


def row(cls="lab-fill-backlog-empty", key="host-a", decision=False, **kw):
    fields = {
        "source": "throughput_tick",
        "evidence": "MISSING lab-fill-backlog | 14 free slots",
        "tried": "filled from the pillar backlog; it was spent",
        "proposed_fix": "queue the M4 board reds",
        **kw,
    }
    return models.ModelExceptionRow(cls=cls, key=key, decision=decision, **fields)


def fake_runner(tmp_path: Path, code: int = 0) -> tuple[Path, Path]:
    seen = tmp_path / "runner-argv.jsonl"
    runner = tmp_path / "fake_runner.py"
    runner.write_text(
        "import json, sys\n"
        f"open({str(seen)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"print('DETACHED lane=x')\nsys.exit({code})\n"
    )
    return runner, seen


def emit(tmp_path, r, runner=None, now="2026-10-19T00:30:00Z"):
    return handler.HandlerExceptionEmit().handle(
        models.ModelExceptionEmitRequest(
            tmp_path / "q", r, now, runner, "orchestrator-bb1a"
        )
    )


def queue(tmp_path, audience):
    return handler.HandlerExceptionQueue().handle(
        models.ModelExceptionQueueRequest(tmp_path / "q", audience)
    )


def test_a_typed_exception_is_one_queue_row_and_dispatches_nothing_the_first_time(
    tmp_path,
):
    runner, seen = fake_runner(tmp_path)
    result = emit(tmp_path, row(), runner)
    assert (result.status, result.occurrence, result.mechanize) == ("appended", 1, "")
    rows = [
        json.loads(line)
        for line in (tmp_path / "q" / handler.QUEUE_FILE).read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["cls"] == "lab-fill-backlog-empty"
    assert rows[0]["id"] == result.id
    assert {"evidence", "tried", "proposed_fix", "decision", "source", "at"} <= set(
        rows[0]
    )
    assert not seen.exists()


def test_an_exception_missing_what_it_tried_or_its_proposed_fix_is_refused_not_queued(
    tmp_path,
):
    result = emit(tmp_path, row(tried="", proposed_fix=" "))
    assert result.status == "refused"
    assert "tried" in result.detail
    assert "proposed_fix" in result.detail
    assert not (tmp_path / "q" / handler.QUEUE_FILE).exists()
    assert emit(tmp_path, row(cls="Not A Slug")).status == "refused"


def test_the_same_open_case_reported_again_is_one_row_not_a_second_occurrence(tmp_path):
    runner, seen = fake_runner(tmp_path)
    emit(tmp_path, row(), runner)
    again = emit(tmp_path, row(), runner, now="2026-10-19T00:40:00Z")
    assert (again.status, again.occurrence) == ("repeat", 1)
    assert len((tmp_path / "q" / handler.QUEUE_FILE).read_text().splitlines()) == 1
    assert not seen.exists()


def test_the_second_occurrence_of_a_class_dispatches_one_mechanize_lane_through_the_runner(
    tmp_path,
):
    runner, seen = fake_runner(tmp_path)
    emit(tmp_path, row(key="host-a"), runner)
    second = emit(tmp_path, row(key="host-b"), runner, now="2026-10-19T00:50:00Z")
    assert second.occurrence == 2
    assert second.mechanize == "sent:mechanize-lab-fill-backlog-empty"
    argv = [json.loads(line) for line in seen.read_text().splitlines()]
    assert len(argv) == 1
    assert argv[0][:2] == ["run", "--detach"]
    assert argv[0][argv[0].index("--lane") + 1] == "mechanize-lab-fill-backlog-empty"
    assert argv[0][argv[0].index("--ticket") + 1] == "OMN-17427"
    assert argv[0][argv[0].index("--parent") + 1] == "orchestrator-bb1a"
    brief = Path(argv[0][argv[0].index("--brief") + 1]).read_text()
    for needed in (
        "detector",
        "actor",
        "friction replay suite",
        "host-a",
        "host-b",
        "queue the M4 board reds",
    ):
        assert needed in brief
    third = emit(tmp_path, row(key="host-c"), runner, now="2026-10-19T01:00:00Z")
    assert (third.occurrence, third.mechanize) == (
        3,
        "already:mechanize-lab-fill-backlog-empty",
    )
    assert len(seen.read_text().splitlines()) == 1


def test_a_refused_dispatch_is_retried_by_the_next_occurrence_and_shows_as_missing(
    tmp_path,
):
    (tmp_path / "bad").mkdir()
    bad, _ = fake_runner(tmp_path / "bad", code=2)
    emit(tmp_path, row(key="a"), bad)
    second = emit(tmp_path, row(key="b"), bad)
    assert second.mechanize.startswith("unsent:")
    lines = queue(tmp_path, "orchestrator").lines
    assert any(
        line.startswith("MISSING mechanize:lab-fill-backlog-empty") for line in lines
    )
    good, seen = fake_runner(tmp_path)
    assert (
        emit(tmp_path, row(key="c"), good).mechanize
        == "sent:mechanize-lab-fill-backlog-empty"
    )
    assert len(seen.read_text().splitlines()) == 1


def test_the_operator_sees_only_decision_rows_and_the_orchestrator_the_rest(tmp_path):
    emit(tmp_path, row(cls="credential-rotation", key="infisical-dev", decision=True))
    emit(tmp_path, row(cls="lab-fill-backlog-empty", key="host-a"))
    op = queue(tmp_path, "operator")
    assert [r["cls"] for r in op.rows] == ["credential-rotation"]
    assert op.lines[0].startswith("DECISION ")
    orch = queue(tmp_path, "orchestrator")
    assert [r["cls"] for r in orch.rows] == ["lab-fill-backlog-empty"]
    assert orch.lines[0].startswith("EXCEPTION ")


def test_a_resolved_row_leaves_the_queue_and_its_recurrence_counts_as_a_new_occurrence(
    tmp_path,
):
    runner, _seen = fake_runner(tmp_path)
    first = emit(tmp_path, row(key="host-a"), runner)
    done = handler.HandlerExceptionResolve().handle(
        models.ModelExceptionResolveRequest(
            tmp_path / "q",
            first.id,
            "orchestrator-bb1a",
            "filled by hand",
            "2026-10-19T00:35:00Z",
        )
    )
    assert done.status == "resolved"
    assert queue(tmp_path, "orchestrator").rows == []
    again = emit(tmp_path, row(key="host-a"), runner)
    assert (again.status, again.occurrence) == ("appended", 2)
    assert again.mechanize.startswith("sent:")
    missing = handler.HandlerExceptionResolve().handle(
        models.ModelExceptionResolveRequest(
            tmp_path / "q", "no-such-id", "x", "y", "2026-10-19T00:35:00Z"
        )
    )
    assert missing.status == "unknown-id"


def test_no_runner_never_dispatches_and_says_so(tmp_path):
    emit(tmp_path, row(key="a"))
    second = emit(tmp_path, row(key="b"))
    assert second.mechanize == "unsent:no-runner"


def test_the_contract_names_the_handlers_and_their_topics():
    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_exception_queue_effect/contract.yaml"
        ).read_text()
    )
    assert contract["handler"]["module"] == f"{NODE}.handler"
    ops = {
        h["operation"]: h["handler"]["name"]
        for h in contract["handler_routing"]["handlers"]
    }
    assert ops == {
        "emit": "HandlerExceptionEmit",
        "list": "HandlerExceptionQueue",
        "resolve": "HandlerExceptionResolve",
        "read": "HandlerExceptionQueueRead",
    }
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    for h in contract["handler_routing"]["handlers"]:
        assert hasattr(handler, h["handler"]["name"])


def test_the_cli_handler_emits_lists_and_resolves_through_the_node(
    tmp_path, monkeypatch, capsys
):
    runner, _seen = fake_runner(tmp_path)
    monkeypatch.setenv("ONEX_EXCEPTION_RUNNER", str(runner))
    cli = handler.HandlerExceptionCli()
    common = [
        "--state-dir",
        str(tmp_path / "q"),
        "--source",
        "test",
        "--evidence",
        "e",
        "--tried",
        "t",
        "--proposed-fix",
        "p",
        "--parent",
        "orchestrator-test",
    ]

    def run(*argv):
        result = cli.handle(models.ModelExceptionCliRequest(list(argv)))
        capsys.readouterr()
        return result

    first = run("emit", "--class", "stale-clone", "--key", "a", *common)
    assert first.code == 0
    assert first.out.startswith("EXCEPTION-QUEUE appended ")
    second = run("emit", "--class", "stale-clone", "--key", "b", *common)
    assert "occurrence=2" in second.out
    assert "mechanize=sent:mechanize-stale-clone" in second.out
    refused = run(
        "emit",
        "--class",
        "stale-clone",
        "--key",
        "c",
        "--state-dir",
        str(tmp_path / "q"),
        "--source",
        "test",
        "--evidence",
        "e",
    )
    assert refused.code == 3
    assert "tried" in refused.out
    listed = run(
        "list", "--audience", "orchestrator", "--state-dir", str(tmp_path / "q")
    )
    assert listed.code == 0
    assert listed.out.count("EXCEPTION ") == 2
    op = run("list", "--audience", "operator", "--state-dir", str(tmp_path / "q"))
    assert op.out.strip() == "EXCEPTION-QUEUE operator decision=0"


def read(tmp_path):
    return handler.HandlerExceptionQueueRead().handle(
        models.ModelExceptionQueueReadRequest(tmp_path / "q")
    )


def test_the_queue_status_read_shows_the_queue_and_names_each_audiences_next_move(
    tmp_path,
):
    empty = read(tmp_path)
    assert empty.line == "exceptions decision=0 open=0 unmechanized=0"
    assert (empty.reasons, empty.actions) == ([], [])
    emit(tmp_path, row(cls="credential-rotation", key="dev", decision=True))
    emit(tmp_path, row(key="a"))
    emit(tmp_path, row(key="b"))
    full = read(tmp_path)
    assert full.line == "exceptions decision=1 open=2 unmechanized=1"
    assert any("decision row(s) await an operator ruling" in r for r in full.reasons)
    assert any("open exception row(s)" in r for r in full.reasons)
    assert any("lab-fill-backlog-empty" in r for r in full.reasons)
    assert any("/omni:operator-ruling" in a for a in full.actions)
    assert len(full.actions) == len(full.reasons) == 3


def test_an_unreadable_queue_is_a_line_not_a_crash_of_the_read(tmp_path):
    (tmp_path / "q").mkdir()
    (tmp_path / "q" / handler.QUEUE_FILE).write_text("{not json\n")
    assert read(tmp_path).line.startswith("exceptions unreadable: ")
