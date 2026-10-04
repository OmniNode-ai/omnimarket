# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure feedback, turn bounds and malformed replies of the code edit loop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.delegated_code_edit.loop_ports import (
    DelegatedCodeEditPorts,
    deployed_lane_delegate_flags,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    ModelCheckResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
    WorkspacePathError,
    parse_turn_reply,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.turn_protocol import (
    HistoryAction,
    HistoryTurn,
    render_history,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_ACTIONS_PER_TURN,
    MAX_WRITE_BYTES,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("max_turns", [1, 40], ids=["minimum", "ceiling"])
@pytest.mark.parametrize("passes", [False, True], ids=["exhausted", "accepted"])
def test_turn_bounds_run_exactly_the_budget_then_check(
    max_turns: int, passes: bool
) -> None:
    ports = FakePorts(
        [_reply(n, _a("ls")) for n in range(1, max_turns + 2)],
        check_passes=[passes],
    )
    request = _request(max_turns=max_turns)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == (
        EnumCodeEditStatus.ACCEPTED if passes else EnumCodeEditStatus.BUDGET_EXHAUSTED
    )
    assert result.turns == max_turns
    assert ports.delegate_turns == list(range(1, max_turns + 1))
    assert result.delegate_run_ids == tuple(f"run-{n}" for n in ports.delegate_turns)
    assert len(ports.turns) == 1  # The first over-budget turn never executes.
    assert ports.checks_run == ["tests"]
    receipt = ports.receipts[request.correlation_id]
    assert receipt["result"] == result.model_dump(mode="json")
    if not passes:
        assert result.detail == (
            f"the turn cap ({max_turns}) was reached with checks failing"
        )
        assert not result.resumable


@pytest.mark.parametrize("tool", ["run_check", "finish"])
def test_check_failure_is_in_the_next_prompt_and_can_be_fixed(tool: str) -> None:
    failure = "AssertionError: add(2, 3) returned 0, expected 5"
    first = _a("run_check", name="tests") if tool == "run_check" else _a("finish")
    ports = FakePorts(
        [
            _reply(1, first),
            _reply(2, _a("write", file_path="src/m.py", content=FIX), _a("finish")),
        ],
        check_passes=[False, True],
        check_output=failure,
    )

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))

    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.turns == 2
    assert ports.checks_run == ["tests", "tests"]
    assert failure not in ports.prompts[0]
    assert failure in ports.prompts[1]
    assert "tests" in ports.prompts[1]
    if tool == "finish":
        assert "FINISH REFUSED: these checks did not pass:" in ports.prompts[1]
    else:
        assert "failed (exit 1)" in ports.prompts[1]
    calls = ports.transcript["calls"]
    assert calls[0]["status"] == "error"
    assert failure in calls[0]["output"]
    assert calls[-1]["status"] == "ok"
    assert result.checks[0].status == "passed"
    assert ports.files["src/m.py"] == FIX


@pytest.mark.parametrize("glob", ["", "/src/*.py", "../*.py", "src/../*.py"])
def test_escaping_writable_glob_is_refused_before_any_turn(glob: str) -> None:
    with pytest.raises(ValidationError, match="not a worktree-relative path"):
        _request(writable_globs=(glob,))


def test_refused_write_reports_the_writable_glob_to_the_next_turn() -> None:
    ports = FakePorts(
        [
            _reply(1, _a("write", file_path="tests/test_m.py", content="tampered")),
            _reply(2, _a("write", file_path="src/m.py", content=FIX), _a("finish")),
        ],
        files={"src/m.py": "return 0\n", "tests/test_m.py": "original\n"},
        check_passes=[True],
    )

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))

    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.refusals == 1
    assert ports.files["tests/test_m.py"] == "original\n"
    assert ports.writes == ["src/m.py"]
    assert (
        "refused: tests/test_m.py is not writable (writable: src/*.py)"
        in (ports.prompts[1])
    )
    assert ports.transcript["calls"][0]["refused"] is True


@pytest.mark.parametrize("recover", [False, True], ids=["repeat-refusal", "recover"])
def test_onex_delegate_refusal_is_feedback_and_two_refusals_stop(
    tmp_path: Path, recover: bool
) -> None:
    argv_seen: list[list[str]] = []

    def refuse(argv: list[str]) -> subprocess.CompletedProcess[str]:
        argv_seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 3, "", "REFUSED: no bound consumer for deployed-lane"
        )

    delegate_ports = DelegatedCodeEditPorts(
        onex=Path("/bin/onex"),
        state_root=tmp_path,
        delegate_flags=deployed_lane_delegate_flags("dev"),
        run_delegate=refuse,
    )

    class RefusingPorts(FakePorts):
        def delegate(
            self,
            request: ModelDelegatedCodeEditRequest,
            prompt: str,
            response_contract: dict[str, object],
            turn: int,
        ) -> ModelTurnReply:
            if recover and turn == 2:
                return super().delegate(request, prompt, response_contract, turn)
            self.delegate_turns.append(turn)
            self.prompts.append(prompt)
            return delegate_ports.delegate(request, prompt, response_contract, turn)

    ports = RefusingPorts([_reply(2, _a("finish"))], check_passes=[True])
    request = _request(max_turns=40)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == (
        EnumCodeEditStatus.ACCEPTED if recover else EnumCodeEditStatus.DELEGATE_FAILED
    )
    assert result.turns == 2
    assert ports.delegate_turns == [1, 2]
    assert "onex delegate exited 3: REFUSED: no bound consumer" in ports.prompts[1]
    assert len(argv_seen) == (1 if recover else 2)
    assert all(argv[:2] == ["/bin/onex", "delegate"] for argv in argv_seen)
    assert result.delegate_run_ids == (("run-2",) if recover else ())
    assert ports.checks_run == (["tests"] if recover else [])
    receipt = ports.receipts[request.correlation_id]
    assert receipt["turns"][0]["ok"] is False
    assert receipt["turns"][0]["actions"] == []
    assert ports.writes == []


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no JSON here", "not a JSON object"),
        ('{"actions":', "not a JSON object"),
        ('{"actions":[]}', "no non-empty actions list"),
        (
            json.dumps({"actions": [{"tool": "ls"}] * (MAX_ACTIONS_PER_TURN + 1)}),
            "at most",
        ),
        ('{"actions":[42]}', "action 1 is not an object"),
        ('{"actions":[{"tool":"shell"}]}', "unknown tool 'shell'"),
        ('{"actions":[{"tool":"ls","command":"rm"}]}', "takes no command"),
        ('{"actions":[{"tool":"view","path":""}]}', "needs a string path"),
        (
            '{"actions":[{"tool":"write","file_path":"a","content":1}]}',
            "content is not a string",
        ),
        (
            '{"actions":[{"tool":"edit","file_path":"a","old_string":"x","new_string":1}]}',
            "action 1 is malformed",
        ),
        (
            '{"actions":[{"tool":"view","path":"a","offset":true}]}',
            "offset must be an integer >= 1",
        ),
    ],
)
def test_malformed_reply_returns_no_partial_actions(text: str, reason: str) -> None:
    actions, invalid_reason = parse_turn_reply(text)
    assert actions == ()
    assert reason in invalid_reason


def test_parser_skips_a_broken_fence_and_normalises_a_numeric_offset() -> None:
    actions, reason = parse_turn_reply(
        "```json\n{broken}\n```\n```json\n"
        '{"actions":[{"tool":"view","path":"src/m.py","offset":"12"}]}\n```'
    )
    assert reason == ""
    assert actions == (_a("view", path="src/m.py").model_copy(update={"offset": 12}),)


def test_history_restores_legacy_messages_and_structured_view_keys() -> None:
    legacy = HistoryTurn.from_json("old receipt message")
    structured = HistoryTurn(
        2,
        actions=(
            HistoryAction(
                "> view(path='src/m.py') -> ok",
                "[src/m.py:1]\n0001| x",
                ("src/m.py", 1),
            ),
            HistoryAction(
                "> write(file_path='src/m.py') -> ok",
                "wrote src/m.py",
                changed_path="src/m.py",
            ),
        ),
    )
    assert legacy == HistoryTurn(0, message="old receipt message")
    assert HistoryTurn.from_json(structured.to_json()) == structured
    shown = render_history([legacy, structured], 10_000)
    assert "old receipt message\nTURN 2" in shown
    assert "src/m.py changed in turn 2 after this view" in shown


def test_history_drops_oldest_brief_turn_when_even_briefs_do_not_fit() -> None:
    history = [HistoryTurn(1, message="old"), HistoryTurn(2, message="new")]
    assert render_history(history, 4) == "[earlier turns cut to fit]\nnew\n"


def test_check_infrastructure_failure_is_terminal_and_receipted() -> None:
    class BrokenCheckPorts(FakePorts):
        def run_check(
            self, request: ModelDelegatedCodeEditRequest, check: ModelDeclaredCheck
        ) -> ModelCheckResult:
            self.checks_run.append(check.name)
            return ModelCheckResult(
                name=check.name, status="infra_error", output_tail="missing binary"
            )

    ports = BrokenCheckPorts([_reply(1, _a("finish"))])
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.INFRA_ERROR
    assert result.detail == "a declared check could not run: tests"
    assert result.turns == 1
    assert ports.checks_run == ["tests"]
    assert (
        ports.receipts[request.correlation_id]["checks"][0]["status"] == "infra_error"
    )


def test_oversized_write_is_refused_without_mutating_the_file() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "write",
                    file_path="src/m.py",
                    content="é" * (MAX_WRITE_BYTES // 2 + 1),
                ),
            )
        ]
    )
    before = ports.files.copy()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert result.refusals == 1
    assert ports.files == before
    assert ports.writes == []
    assert (
        ports.transcript["calls"][0]["output"]
        == f"refused: content over {MAX_WRITE_BYTES} bytes"
    )


def test_workspace_failure_still_writes_a_terminal_receipt() -> None:
    class BrokenWorkspacePorts(FakePorts):
        def workspace_files(
            self, request: ModelDelegatedCodeEditRequest
        ) -> tuple[tuple[str, int], ...]:
            raise WorkspacePathError("worktree is missing")

    ports = BrokenWorkspacePorts([])
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.INFRA_ERROR
    assert result.detail == "workspace: worktree is missing"
    assert ports.delegate_turns == []
    assert ports.checks_run == []
    assert ports.receipts[request.correlation_id]["error"]["detail"] == result.detail
