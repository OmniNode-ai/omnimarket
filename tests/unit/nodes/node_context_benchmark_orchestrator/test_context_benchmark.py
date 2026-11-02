# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the paired CLAUDE.md / AGENTS.md benchmark node (OMN-21896)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.handler_context_benchmark import (
    HandlerContextBenchmark,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
    CommandResult,
    LaneDelegateRoute,
    LaneJevTypedDecision,
    parse_claude_output,
    parse_codex_output,
    sanitized_env,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.report_rows import (
    build_rows,
    render_markdown,
    sign_test_p,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    UNKNOWN,
    EnumBenchmarkArm,
    EnumBenchmarkLane,
    EnumRunOutcome,
    EnumVerdict,
    ModelBenchmarkLaneSpec,
    ModelBenchmarkTask,
    ModelContextBenchmarkRequest,
    ModelJevQuestion,
    ModelLaneInvocation,
    ModelRunRecord,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_context_benchmark_orchestrator"
)
BIFROST = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/configs/bifrost_delegation.yaml"
)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    (src / "sub").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "CLAUDE.md").write_text("claude guidance\n")
    (src / "AGENTS.md").write_text("agents guidance\n")
    (src / "sub" / "CLAUDE.md").write_text("nested\n")
    (src / "a.py").write_text("x = 1\n")
    (src / "tests" / "check.txt").write_text("pristine\n")
    return src


def _task(
    task_id: str = "t1", jev_question: ModelJevQuestion | None = None
) -> ModelBenchmarkTask:
    return ModelBenchmarkTask(
        task_id=task_id,
        prompt="do it",
        check_argv=("check",),
        edit_files=("a.py",),
        protected_paths=("tests",),
        jev_question=jev_question,
    )


def _request(
    source: Path, tmp_path: Path, lanes, tasks=None
) -> ModelContextBenchmarkRequest:
    return ModelContextBenchmarkRequest(
        source_dir=str(source),
        tasks=tuple(tasks or [_task("t1"), _task("t2")]),
        lanes=tuple(lanes),
        scratch_dir=str(tmp_path / "scratch"),
        host="h-test",
    )


class _Recorder:
    """A lane runner that records what the copy held when it was called."""

    def __init__(self, tokens: bool = True) -> None:
        self.seen: list[tuple[str, dict[str, bool]]] = []
        self._tokens = tokens

    def invoke(self, *, spec, task, workdir, context_text, timeout):
        self.seen.append(
            (
                workdir.name,
                {
                    "CLAUDE.md": (workdir / "CLAUDE.md").exists(),
                    "AGENTS.md": (workdir / "AGENTS.md").exists(),
                    "nested": (workdir / "sub" / "CLAUDE.md").exists(),
                    "context_text": bool(context_text),
                },
            )
        )
        (workdir / "tests" / "check.txt").write_text("tampered\n")
        if not self._tokens:
            return ModelLaneInvocation()
        return ModelLaneInvocation(
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=100,
            cache_write_tokens=0,
        )


def _check_fn(calls: list[Path]):
    def run(argv, cwd, timeout, stdin, env):
        calls.append(cwd)
        pristine = (cwd / "tests" / "check.txt").read_text() == "pristine\n"
        return CommandResult(0 if pristine else 1, "", "")

    return run


def test_context_benchmark_arms_remove_context_in_copy_only(
    source: Path, tmp_path: Path
) -> None:
    claude, codex = _Recorder(), _Recorder(tokens=False)
    checks: list[Path] = []
    handler = HandlerContextBenchmark(
        runners={
            EnumBenchmarkLane.CLAUDE_HEADLESS: claude,
            EnumBenchmarkLane.CODEX_HEADLESS: codex,
        },
        command_fn=_check_fn(checks),
    )
    lanes = [
        ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.CLAUDE_HEADLESS, model_id="claude"
        ),
        ModelBenchmarkLaneSpec(lane=EnumBenchmarkLane.CODEX_HEADLESS, model_id="codex"),
    ]
    result = handler.handle(_request(source, tmp_path, lanes))

    for recorder in (claude, codex):
        by_arm = dict(recorder.seen)
        assert by_arm["on"] == {
            "CLAUDE.md": True,
            "AGENTS.md": True,
            "nested": True,
            "context_text": True,
        }
        assert by_arm["off"] == {
            "CLAUDE.md": False,
            "AGENTS.md": False,
            "nested": False,
            "context_text": False,
        }
    # the source tree is never touched
    assert (source / "CLAUDE.md").exists()
    assert (source / "AGENTS.md").exists()
    assert (source / "sub" / "CLAUDE.md").exists()
    # one run per task, arm and lane, all on the same source sha
    assert len(result.runs) == 2 * 2 * 2
    assert len({r.source_sha for r in result.runs}) == 1
    # the task's own check decided the outcome (protected files restored)
    assert {r.outcome for r in result.runs} == {EnumRunOutcome.PASS}
    claude_run = next(r for r in result.runs if r.model_id == "claude")
    assert (claude_run.input_tokens, claude_run.output_tokens) == (10, 5)
    assert (claude_run.cache_read_tokens, claude_run.cache_write_tokens) == (100, 0)
    # an absent token count is the string UNKNOWN, never 0
    codex_run = next(r for r in result.runs if r.model_id == "codex")
    assert codex_run.input_tokens == UNKNOWN == codex_run.cache_read_tokens


def test_context_benchmark_arms_on_arm_without_context_is_an_error(
    source: Path, tmp_path: Path
) -> None:
    (source / "AGENTS.md").unlink()
    handler = HandlerContextBenchmark(
        runners={EnumBenchmarkLane.CLAUDE_HEADLESS: _Recorder()},
        command_fn=_check_fn([]),
    )
    result = handler.handle(
        _request(
            source,
            tmp_path,
            [
                ModelBenchmarkLaneSpec(
                    lane=EnumBenchmarkLane.CLAUDE_HEADLESS, model_id="c"
                )
            ],
            [_task("t1")],
        )
    )
    on = next(r for r in result.runs if r.arm is EnumBenchmarkArm.ON)
    assert on.outcome is EnumRunOutcome.ERROR
    assert "AGENTS.md" in on.detail


def test_context_benchmark_arms_headless_commands_and_usage_parsing() -> None:
    claude_json = json.dumps(
        {
            "is_error": False,
            "result": "done",
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "cache_creation_input_tokens": 11,
                "cache_read_input_tokens": 13,
            },
            "modelUsage": {"claude-sonnet-5-5": {}},
        }
    )
    got = parse_claude_output(CommandResult(0, claude_json + "\n", ""))
    assert (
        got.input_tokens,
        got.output_tokens,
        got.cache_write_tokens,
        got.cache_read_tokens,
    ) == (7, 3, 11, 13)
    assert got.served_model == "claude-sonnet-5-5"
    assert parse_claude_output(
        CommandResult(0, json.dumps({"is_error": True, "result": "Not logged in"}), "")
    ).error

    codex_events = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "thread.started", "thread_id": "x"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "pong"},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 18113,
                    "cached_input_tokens": 12288,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 5,
                },
            },
        )
    )
    got = parse_codex_output(CommandResult(0, codex_events, ""))
    assert (got.input_tokens, got.cache_read_tokens, got.output_tokens) == (
        18113 - 12288,
        12288,
        5,
    )
    assert parse_codex_output(
        CommandResult(0, "", "")
    ).error  # no usage event: not 0 tokens

    seen: list[tuple[str, ...]] = []

    def capture(argv, cwd, timeout, stdin, env):
        seen.append(tuple(argv))
        return CommandResult(0, codex_events, "")

    from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
        LaneCodexHeadless,
    )

    LaneCodexHeadless(capture).invoke(
        spec=ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.CODEX_HEADLESS, model_id="c"
        ),
        task=_task(),
        workdir=Path("/tmp"),
        context_text=None,
        timeout=5,
    )
    argv = seen[0]
    assert argv[:2] == ("codex", "exec")
    assert 'model_reasoning_effort="high"' in argv
    assert "-m" not in argv
    assert "--model" not in argv


def _jev_task(task_id: str, answer: str = "a") -> ModelBenchmarkTask:
    return _task(
        task_id,
        jev_question=ModelJevQuestion(
            instructions="pick",
            options={"a": "first", "b": "second"},
            expected_answer=answer,
        ),
    )


def test_context_benchmark_jev_secret_ref_resolved_from_credential_store_not_mcp() -> (
    None
):
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    bifrost = yaml.safe_load(BIFROST.read_text())
    backend = next(
        b for b in _iter_backends(bifrost) if b["backend_id"] == "cloud-typesafe-jev"
    )
    assert (
        contract["jev"]["secret_ref"] == backend["secret_ref"] == "llm.typesafe.api_key"
    )
    assert contract["jev"]["backend_id"] == "cloud-typesafe-jev"
    assert contract["jev"]["resolution"] == "delegation_credential_store"
    assert contract["jev"]["via_mcp"] is False


def _iter_backends(doc):
    if isinstance(doc, dict):
        if "backend_id" in doc:
            yield doc
        for v in doc.values():
            yield from _iter_backends(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _iter_backends(v)


def test_context_benchmark_jev_secret_ref_never_reaches_env_request_or_receipt(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "SENTINEL-KEY-VALUE-0123456789"
    monkeypatch.setenv("TYPESAFE_API_KEY", sentinel)
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    calls: list[dict[str, object]] = []

    def node(argv, cwd, timeout, stdin, env):
        calls.append({"argv": tuple(argv), "stdin": stdin, "env": dict(env or {})})
        error = f"upstream said Authorization: Bearer {sentinel}"
        return CommandResult(
            3,
            json.dumps(
                {
                    "decided_by": "incumbent_backend_error",
                    "reason": "backend_http_error",
                    "detail": error,
                }
            ),
            "",
        )

    handler = HandlerContextBenchmark(command_fn=node)
    result = handler.handle(
        _request(
            source,
            tmp_path,
            [
                ModelBenchmarkLaneSpec(
                    lane=EnumBenchmarkLane.JEV_TYPED_DECISION, model_id="jev-latest"
                )
            ],
            [_jev_task("j1")],
        )
    )
    assert calls
    assert all(
        c["argv"][-1] == "omnimarket.nodes.node_typed_decision_effect" for c in calls
    )
    for call in calls:
        assert sentinel not in json.dumps(call["env"])
        assert sentinel not in str(call["stdin"])
        assert not any(
            k in call["env"]
            for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH")
        )
    assert sentinel not in result.model_dump_json()
    assert sentinel not in sanitized_env(
        {"TYPESAFE_API_KEY": sentinel, "PATH": "/bin"}
    ).get("TYPESAFE_API_KEY", "")


def test_context_benchmark_jev_secret_ref_absent_credential_blocks_lane_with_host(
    source: Path, tmp_path: Path
) -> None:
    calls = 0

    def node(argv, cwd, timeout, stdin, env):
        nonlocal calls
        calls += 1
        return CommandResult(
            3,
            json.dumps(
                {
                    "decided_by": "incumbent_refused",
                    "reason": "credential_not_registered",
                }
            ),
            "",
        )

    result = HandlerContextBenchmark(command_fn=node).handle(
        _request(
            source,
            tmp_path,
            [
                ModelBenchmarkLaneSpec(
                    lane=EnumBenchmarkLane.JEV_TYPED_DECISION, model_id="jev-latest"
                )
            ],
            [_jev_task("j1"), _jev_task("j2")],
        )
    )
    assert calls == 1  # the lane stops at the first refusal
    assert {r.outcome for r in result.runs} == {EnumRunOutcome.BLOCKED}
    assert all(
        "credential-absent" in r.detail and "h-test" in r.detail for r in result.runs
    )
    assert result.blocked_lanes
    assert "h-test" in result.blocked_lanes[0]
    assert all(r.outcome is not EnumRunOutcome.FAIL for r in result.runs)


def test_context_benchmark_jev_secret_ref_untyped_task_is_not_applicable_not_failed(
    source: Path, tmp_path: Path
) -> None:
    answers = iter(["a", "b"])

    def node(argv, cwd, timeout, stdin, env):
        body = json.loads(stdin)
        assert ("repository_guidance" in body["state"]) in (True, False)
        return CommandResult(
            0,
            json.dumps(
                {
                    "decided_by": "model",
                    "model_answer": next(answers),
                    "input_tokens": 20,
                    "output_tokens": 2,
                    "served_model": "jev-1.13.0",
                }
            ),
            "",
        )

    result = HandlerContextBenchmark(command_fn=node).handle(
        _request(
            source,
            tmp_path,
            [
                ModelBenchmarkLaneSpec(
                    lane=EnumBenchmarkLane.JEV_TYPED_DECISION, model_id="jev-latest"
                )
            ],
            [_jev_task("typed"), _task("agent")],
        )
    )
    typed = [r for r in result.runs if r.task_id == "typed"]
    agent = [r for r in result.runs if r.task_id == "agent"]
    assert {r.outcome for r in agent} == {EnumRunOutcome.NOT_APPLICABLE}
    assert all("typed decisions" in r.detail for r in agent)
    assert sorted(r.outcome.value for r in typed) == ["fail", "pass"]
    on = next(r for r in result.rows if r.condition is EnumBenchmarkArm.ON)
    assert on.not_applicable == 1
    assert on.tasks_run == 1


def test_context_benchmark_jev_secret_ref_guidance_only_in_on_arm(
    source: Path, tmp_path: Path
) -> None:
    states: dict[str, bool] = {}
    runner = LaneJevTypedDecision(
        command_fn=lambda _argv, _cwd, _t, stdin, _env: (
            states.setdefault(
                str("repository_guidance" in json.loads(stdin)["state"]), True
            ),
            CommandResult(
                0, json.dumps({"decided_by": "model", "model_answer": "a"}), ""
            ),
        )[1]
    )
    spec = ModelBenchmarkLaneSpec(
        lane=EnumBenchmarkLane.JEV_TYPED_DECISION, model_id="j"
    )
    runner.invoke(
        spec=spec,
        task=_jev_task("x"),
        workdir=source,
        context_text="guidance",
        timeout=5,
    )
    runner.invoke(
        spec=spec, task=_jev_task("x"), workdir=source, context_text=None, timeout=5
    )
    assert states == {"True": True, "False": True}


def _fake_delegate(
    state_root: Path,
    answer_for_on: str,
    seen_prompts: list[str],
    seen_argv: list[tuple[str, ...]],
):
    def run(argv, cwd, timeout, stdin, env):
        seen_argv.append(tuple(argv))
        seen_prompts.append(argv[2])
        run_id = f"00000000-0000-4000-8000-{len(seen_prompts):012d}"
        d = state_root / "runs" / run_id
        d.mkdir(parents=True)
        (d / "result.txt").write_text(
            answer_for_on if "Repository guidance" in argv[2] else "no files here"
        )
        (d / "workflow_result.json").write_text(
            json.dumps(
                {
                    "terminal_payload": {
                        "payload": {
                            "model_name": "Qwen3.8-27B",
                            "metrics": {"input_tokens": 900, "output_tokens": 40},
                        }
                    }
                }
            )
        )
        return CommandResult(0, f"delegate artifacts: {d}/result.txt\n", "")

    return run


def test_context_benchmark_models_qwen_and_a_further_model_run_both_conditions(
    source: Path, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    prompts: list[str] = []
    argvs: list[tuple[str, ...]] = []
    route = LaneDelegateRoute(
        onex_bin="onex",
        state_root=state_root,
        lane="lane",
        ticket="OMN-21896",
        command_fn=_fake_delegate(
            state_root, "### a.py\n```python\nx = 2\n```\n", prompts, argvs
        ),
    )

    def check(argv, cwd, timeout, stdin, env):
        return CommandResult(
            0 if (cwd / "a.py").read_text() == "x = 2\n" else 1, "", ""
        )

    handler = HandlerContextBenchmark(
        runners={EnumBenchmarkLane.DELEGATE_ROUTE: route}, command_fn=check
    )
    lanes = [
        ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.DELEGATE_ROUTE, model_id="Qwen3.8-27B"
        ),
        ModelBenchmarkLaneSpec(
            lane=EnumBenchmarkLane.DELEGATE_ROUTE,
            model_id="openrouter-north-mini-code",
            backend_id="openrouter-north-mini-code",
        ),
    ]
    result = handler.handle(
        _request(source, tmp_path, lanes, [_task("t1"), _task("t2")])
    )

    assert {r.model_id for r in result.runs} == {
        "Qwen3.8-27B",
        "openrouter-north-mini-code",
    }
    assert {r.arm for r in result.runs} == {EnumBenchmarkArm.ON, EnumBenchmarkArm.OFF}
    assert len(result.runs) == 2 * 2 * 2
    pinned = [a for a in argvs if "--backend-id" in a]
    assert pinned
    assert all(
        a[a.index("--backend-id") + 1] == "openrouter-north-mini-code" for a in pinned
    )
    assert (
        len(pinned) == len(argvs) // 2
    )  # the Qwen arm names no pin: it takes the lab rung
    # the guidance rides in the prompt of the on arm only
    on_prompts = [p for p in prompts if "Repository guidance" in p]
    assert len(on_prompts) == len(prompts) // 2
    assert "claude guidance" in on_prompts[0]
    # on arm applied the returned file and passed; off arm returned no file block and failed
    for r in result.runs:
        assert r.outcome is (
            EnumRunOutcome.PASS if r.arm is EnumBenchmarkArm.ON else EnumRunOutcome.FAIL
        )
        assert (r.input_tokens, r.output_tokens) == (900, 40)
        assert r.cache_read_tokens == UNKNOWN  # the route reports no cache split
        assert r.served_model == "Qwen3.8-27B"


def test_context_benchmark_models_route_returning_unlisted_file_changes_nothing(
    source: Path, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
        apply_file_blocks,
    )

    work = tmp_path / "w"
    (work / "tests").mkdir(parents=True)
    (work / "tests" / "check.txt").write_text("keep")
    out = "### tests/check.txt\n```\nhacked\n```\n"
    assert apply_file_blocks(out, _task(), work) == 0
    assert (work / "tests" / "check.txt").read_text() == "keep"


def _run(
    model: str,
    task: str,
    arm: EnumBenchmarkArm,
    outcome: EnumRunOutcome,
    tokens: int | str = 100,
) -> ModelRunRecord:
    return ModelRunRecord(
        task_id=task,
        lane=EnumBenchmarkLane.CODEX_HEADLESS,
        model_id=model,
        arm=arm,
        source_sha="abc",
        outcome=outcome,
        input_tokens=tokens,
        output_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _paired(
    model: str, n: int, on_wins: int, tokens_on: int = 120, tokens_off: int = 100
):
    runs = []
    for i in range(n):
        on_pass = i < on_wins
        runs.append(
            _run(
                model,
                f"t{i}",
                EnumBenchmarkArm.ON,
                EnumRunOutcome.PASS if on_pass else EnumRunOutcome.FAIL,
                tokens_on,
            )
        )
        runs.append(
            _run(
                model,
                f"t{i}",
                EnumBenchmarkArm.OFF,
                EnumRunOutcome.FAIL if on_pass else EnumRunOutcome.PASS,
                tokens_off,
            )
        )
    return runs


def test_context_benchmark_report_underpowered_below_ten_pairs_has_no_verdict() -> None:
    rows = build_rows(tuple(_paired("m9", 9, 9)), min_paired_tasks=10)
    assert len(rows) == 2
    assert {r.condition for r in rows} == {
        EnumBenchmarkArm.ON,
        EnumBenchmarkArm.OFF,
    }
    assert all(
        r.underpowered and r.verdict is None and r.paired_tasks == 9 for r in rows
    )
    assert "yes (no verdict)" in render_markdown(rows, min_paired_tasks=10)


def test_context_benchmark_report_row_per_model_and_condition_with_paired_difference() -> (
    None
):
    runs = _paired("m12", 12, 12) + _paired("other", 10, 0)
    rows = build_rows(tuple(runs), min_paired_tasks=10)
    assert len(rows) == 4
    on = next(
        r for r in rows if r.model_id == "m12" and r.condition is EnumBenchmarkArm.ON
    )
    off = next(
        r for r in rows if r.model_id == "m12" and r.condition is EnumBenchmarkArm.OFF
    )
    assert (on.tasks_run, on.passes, off.passes) == (12, 12, 0)
    assert on.mean_tokens == 130
    assert off.mean_tokens == 110
    assert on.paired_pass_rate_diff == 1.0
    assert on.paired_mean_tokens_diff == 20
    assert not on.underpowered
    assert on.verdict is EnumVerdict.CONTEXT_HELPS
    hurts = next(r for r in rows if r.model_id == "other")
    assert hurts.verdict is EnumVerdict.CONTEXT_HURTS


def test_context_benchmark_report_unknown_tokens_are_unknown_not_zero_and_unpaired_runs_excluded() -> (
    None
):
    runs = [
        _run("m", "a", EnumBenchmarkArm.ON, EnumRunOutcome.PASS, UNKNOWN),
        _run("m", "a", EnumBenchmarkArm.OFF, EnumRunOutcome.PASS, UNKNOWN),
        _run("m", "b", EnumBenchmarkArm.ON, EnumRunOutcome.NOT_APPLICABLE),
        _run("m", "b", EnumBenchmarkArm.OFF, EnumRunOutcome.NOT_APPLICABLE),
        _run("m", "c", EnumBenchmarkArm.ON, EnumRunOutcome.PASS),
    ]
    rows = build_rows(tuple(runs), min_paired_tasks=10)
    on = next(r for r in rows if r.condition is EnumBenchmarkArm.ON)
    assert on.mean_tokens == 110  # only the run with known tokens counts
    assert on.paired_tasks == 1
    assert on.paired_mean_tokens_diff == UNKNOWN
    assert on.not_applicable == 1
    assert on.tasks_run == 2


def test_context_benchmark_report_sign_test_is_exact() -> None:
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(10, 0) == pytest.approx(2 / 1024)
    assert sign_test_p(3, 3) == 1.0
