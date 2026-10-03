# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegated code edit loop, driven through fake ports (OMN-20290 AC1)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any, cast

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    MAX_ERROR_CHARS,
    RESPONSE_CONTRACT,
    EnumCodeEditStatus,
    EnumCodeEditTool,
    HandlerDelegatedCodeEditOrchestrator,
    LoopReceiptExistsError,
    ModelCheckResult,
    ModelCodeEditAction,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
    ResumeRefusedError,
    WorkspacePathError,
    bound_error,
    normalise_path,
    writable,
)

pytestmark = pytest.mark.unit


def _request(**overrides: object) -> ModelDelegatedCodeEditRequest:
    fields: dict[str, object] = {
        "correlation_id": str(uuid.uuid4()),
        "task": "make add() return the sum",
        "workspace_root": "/work/tree",
        "writable_globs": ("src/*.py",),
        "checks": (
            ModelDeclaredCheck(
                name="tests",
                argv=("pytest", "tests/test_m.py"),
                targets=("tests/test_m.py",),
            ),
        ),
        "max_turns": 6,
    }
    fields.update(overrides)
    return ModelDelegatedCodeEditRequest.model_validate(fields)


def _a(tool: str, **kw: str) -> ModelCodeEditAction:
    return ModelCodeEditAction(tool=EnumCodeEditTool(tool), **kw)


class FakePorts:
    def __init__(
        self,
        turns: Sequence[ModelTurnReply],
        files: dict[str, str] | None = None,
        check_passes: Sequence[bool] = (),
        check_output: str = "1 failed",
    ) -> None:
        self.turns = list(turns)
        self.files = dict(files or {"src/m.py": "def add(a, b):\n    return 0\n"})
        self.check_passes = list(check_passes)
        self.check_output = check_output
        self.receipts: dict[str, dict[str, object]] = {}
        self.claimed: set[str] = set()
        self.archived: list[dict[str, object]] = []
        self.state_root = "/state"
        self.delegate_turns: list[int] = []
        self.writes: list[str] = []
        self.checks_run: list[str] = []
        self.formatter_argv: list[tuple[str, ...]] = []
        self.prompts: list[str] = []
        self.contracts: list[dict[str, object]] = []

    def claim_loop_receipt(self, loop_run_id: str, *, resume: bool = False) -> None:
        if loop_run_id in self.claimed:
            raise LoopReceiptExistsError("already claimed")
        if resume:
            if loop_run_id not in self.receipts:
                raise ResumeRefusedError("no receipt")
            self.archived.append(self.receipts.pop(loop_run_id))
        elif loop_run_id in self.receipts:
            raise LoopReceiptExistsError(loop_run_id)
        self.claimed.add(loop_run_id)

    def load_loop_receipt(self, loop_run_id: str) -> dict[str, object] | None:
        return self.receipts.get(loop_run_id)

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        self.receipts[loop_run_id] = payload
        self.claimed.discard(loop_run_id)

    def workspace_files(
        self, request: ModelDelegatedCodeEditRequest
    ) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((p, b.count("\n")) for p, b in self.files.items()))

    def read_file(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        if path not in self.files:
            raise WorkspacePathError(f"{path} is not a file")
        return self.files[path]

    def list_dir(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        return "\n".join(sorted(self.files))

    def grep(
        self, request: ModelDelegatedCodeEditRequest, pattern: str, path: str
    ) -> str:
        return (
            "\n".join(
                f"{p}:1:{line}"
                for p, b in self.files.items()
                for line in b.splitlines()
                if pattern in line
            )
            or "no matches"
        )

    def write_file(
        self, request: ModelDelegatedCodeEditRequest, path: str, content: str
    ) -> None:
        self.writes.append(path)
        self.files[path] = content

    def run_check(
        self, request: ModelDelegatedCodeEditRequest, check: ModelDeclaredCheck
    ) -> ModelCheckResult:
        if check.name == "format":
            self.formatter_argv.append(check.argv)
            return ModelCheckResult(name="format", status="passed", exit_code=0)
        self.checks_run.append(check.name)
        passed = self.check_passes.pop(0) if self.check_passes else False
        return ModelCheckResult(
            name=check.name,
            status="passed" if passed else "failed",
            exit_code=0 if passed else 1,
            output_tail="1 passed" if passed else self.check_output,
            fingerprint="" if passed else "fp-" + self.check_output,
        )

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        self.delegate_turns.append(turn)
        self.prompts.append(prompt)
        self.contracts.append(response_contract)
        if not self.turns:
            return ModelTurnReply(
                run_id=f"run-{turn}", ok=True, actions=(_a("ls"),), raw_text="{}"
            )
        return self.turns.pop(0)

    def diff(self, request: ModelDelegatedCodeEditRequest) -> str:
        changed = sorted(set(self.writes))
        return "".join(f"--- a/{p}\n+++ b/{p}\n@@\n" for p in changed)

    def score(
        self, request: ModelDelegatedCodeEditRequest, transcript: dict[str, object]
    ) -> dict[str, object]:
        self.transcript = transcript
        return {"verdict": {"outcome": "PASS"}}


def _reply(n: int, *actions: ModelCodeEditAction) -> ModelTurnReply:
    return ModelTurnReply(run_id=f"run-{n}", ok=True, actions=actions, raw_text="{...}")


FIX = "def add(a, b):\n    return a + b\n"


@pytest.mark.parametrize(
    "text",
    ["", "UndefinedTable", "x" * MAX_ERROR_CHARS],
    ids=["empty", "short", "at-limit"],
)
def test_bound_error_leaves_short_text_unchanged(text: str) -> None:
    assert bound_error(text) == text


@pytest.mark.parametrize("limit", [MAX_ERROR_CHARS, 300, 64])
def test_bound_error_keeps_head_and_tail_with_the_exact_cut_count(limit: int) -> None:
    head, tail = "HEADMARK", "UndefinedTable"
    text = head + "x" * (10_000 - len(head) - len(tail)) + tail
    bounded = bound_error(text, limit)
    assert len(bounded) <= limit
    assert bounded.startswith(head)
    assert bounded.endswith(tail)
    match = re.fullmatch(
        r"(.*)\n\.\.\. \[(\d+) characters cut\] \.\.\.\n(.*)",
        bounded,
        re.DOTALL,
    )
    assert match is not None
    kept_head, cut, kept_tail = match.groups()
    assert int(cut) == len(text) - len(kept_head) - len(kept_tail)
    share = len(kept_head) + len(kept_tail)
    assert len(kept_head) == (share + 1) // 2
    assert len(kept_tail) == share // 2
    assert text.startswith(kept_head)
    assert text.endswith(kept_tail)


@pytest.mark.parametrize("limit", [0, 10, 63])
def test_bound_error_uses_a_prefix_for_tiny_limits(limit: int) -> None:
    text = "HEADMARK" + "x" * 100
    assert bound_error(text, limit) == text[:limit]


def test_accepted_when_finish_checks_pass_and_every_turn_has_a_run_id() -> None:
    ports = FakePorts(
        [
            _reply(1, _a("view", path="src/m.py")),
            _reply(
                2,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
                _a("finish", summary="add returns a + b; tests/test_m.py passes"),
            ),
        ],
        check_passes=[True],
    )
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.delegate_run_ids == ("run-1", "run-2")
    assert result.changed_paths == ("src/m.py",)
    assert ports.files["src/m.py"] == FIX
    assert result.rubric_outcome == "PASS"
    receipt = ports.receipts[request.correlation_id]
    assert receipt["error"] is None
    assert receipt["delegate_run_ids"] == ["run-1", "run-2"]
    assert receipt["rubric_verdict"] == {"verdict": {"outcome": "PASS"}}
    assert all(c == RESPONSE_CONTRACT for c in ports.contracts)
    assert [t["run_id"] for t in receipt["turns"]] == ["run-1", "run-2"]  # type: ignore[index]


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "/etc/passwd",
        "../outside.py",
        "src/../../x.py",
        ".git/config",
        "src/sub/deep.py",
    ],
)
def test_write_outside_writable_globs_is_refused_and_nothing_is_written(
    path: str,
) -> None:
    ports = FakePorts(
        [_reply(1, _a("write", file_path=path, content="x = 1\n"))],
        check_passes=[False, False],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    assert result.refusals == 1
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED


def test_edit_outside_writable_globs_is_refused() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a("edit", file_path="tests/test_m.py", old_string="a", new_string="b"),
            )
        ],
        files={"src/m.py": "x\n", "tests/test_m.py": "a\n"},
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    assert ports.files["tests/test_m.py"] == "a\n"
    assert result.refusals == 1


def test_undeclared_check_is_refused_and_not_run() -> None:
    ports = FakePorts([_reply(1, _a("run_check", name="rm-rf"))])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    # Only the turn-cap check round ran, never the undeclared name.
    assert ports.checks_run == ["tests"]
    assert result.refusals == 1


def test_two_identical_failing_check_rounds_over_the_same_diff_stop_with_no_progress() -> (
    None
):
    finish = _a("finish", summary="done")
    ports = FakePorts([_reply(1, finish), _reply(2, finish), _reply(3, finish)])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=6))
    assert result.status == EnumCodeEditStatus.NO_PROGRESS
    assert result.turns == 2


def test_a_changed_diff_between_failing_rounds_is_progress() -> None:
    ports = FakePorts(
        [
            _reply(1, _a("write", file_path="src/a.py", content="1\n"), _a("finish")),
            _reply(2, _a("write", file_path="src/b.py", content="2\n"), _a("finish")),
        ],
        check_passes=[False, False, False],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert result.turns == 2


def test_turn_cap_ends_budget_exhausted_unless_the_final_checks_pass() -> None:
    ports = FakePorts([], check_passes=[False])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=3))
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert result.turns == 3
    ports = FakePorts([], check_passes=[True])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=3))
    assert result.status == EnumCodeEditStatus.ACCEPTED


def test_two_failed_delegate_runs_in_a_row_end_delegate_failed() -> None:
    dead = ModelTurnReply(run_id="", ok=False, invalid_reason="onex delegate exited 3")
    ports = FakePorts([dead, dead])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert ports.checks_run == []


def test_failed_delegate_errors_keep_the_head_and_tail_in_the_receipt() -> None:
    head, tail = "HEADMARK", "UndefinedTable"
    reason = head + "x" * (6000 - len(head) - len(tail)) + tail
    ports = FakePorts(
        [
            ModelTurnReply(run_id=f"run-{n}", ok=False, invalid_reason=reason)
            for n in (1, 2)
        ]
    )
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert len(result.detail) <= 300
    assert tail in result.detail
    receipt = ports.receipts[request.correlation_id]
    error = cast(dict[str, object], receipt["error"])
    assert error["status"] == "delegate_failed"
    assert error["turns"] == 2
    detail = cast(str, error["detail"])
    assert detail.startswith("two delegate runs failed in a row: HEADMARK")
    assert detail.endswith(tail)
    assert len(detail) <= MAX_ERROR_CHARS
    turns = cast(list[dict[str, object]], receipt["turns"])
    assert len(turns) == 2
    for turn in turns:
        invalid_reason = cast(str, turn["invalid_reason"])
        assert invalid_reason.startswith(head)
        assert invalid_reason.endswith(tail)
        assert len(invalid_reason) <= MAX_ERROR_CHARS


def test_an_unexpected_delegate_exception_still_writes_exactly_one_receipt() -> None:
    class BrokenPorts(FakePorts):
        receipt_writes = 0

        def delegate(
            self,
            request: ModelDelegatedCodeEditRequest,
            prompt: str,
            response_contract: dict[str, object],
            turn: int,
        ) -> ModelTurnReply:
            raise AttributeError("'NoneType' object has no attribute 'get'")

        def write_loop_receipt(
            self, loop_run_id: str, payload: dict[str, object]
        ) -> None:
            self.receipt_writes += 1
            super().write_loop_receipt(loop_run_id, payload)

    ports = BrokenPorts([])
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.INFRA_ERROR
    assert (
        result.detail
        == "unexpected AttributeError: 'NoneType' object has no attribute 'get'"
    )
    assert ports.receipt_writes == 1
    assert list(ports.receipts) == [request.correlation_id]


@pytest.mark.parametrize("failed_port", ["diff", "score"])
def test_diff_and_score_exceptions_still_write_a_receipt(failed_port: str) -> None:
    class BrokenPorts(FakePorts):
        def diff(self, request: ModelDelegatedCodeEditRequest) -> str:
            if failed_port == "diff":
                raise RuntimeError("boom")
            return super().diff(request)

        def score(
            self, request: ModelDelegatedCodeEditRequest, transcript: dict[str, object]
        ) -> dict[str, object]:
            if failed_port == "score":
                raise RuntimeError("boom")
            return super().score(request, transcript)

    ports = BrokenPorts([_reply(1, _a("finish"))], check_passes=[True])
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert list(ports.receipts) == [request.correlation_id]
    receipt = ports.receipts[request.correlation_id]
    if failed_port == "diff":
        assert result.detail == "diff failed: RuntimeError: boom"
        assert receipt["diff"] == ""
    else:
        assert result.detail == ""
        assert receipt["rubric_verdict"] == {"error": "RuntimeError: boom"}


def test_an_unusable_reply_is_fed_back_not_terminal() -> None:
    bad = ModelTurnReply(
        run_id="run-1", ok=False, invalid_reason="not JSON", raw_text="hello"
    )
    ports = FakePorts([bad, _reply(2, _a("finish"))], check_passes=[True])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert "your reply was unusable: not JSON" in ports.prompts[1]


def test_edit_needs_exactly_one_occurrence() -> None:
    ports = FakePorts(
        [_reply(1, _a("edit", file_path="src/m.py", old_string="a", new_string="z"))],
        files={"src/m.py": "a a\n"},
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    assert (
        "occurs 2 times"
        in ports.receipts[next(iter(ports.receipts))]["turns"][0]["actions"][0][
            "output"
        ]
    )  # type: ignore[index]


def test_a_rerun_of_the_same_correlation_is_refused_before_any_turn() -> None:
    ports = FakePorts([], check_passes=[True])
    request = _request(max_turns=1)
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    turns_before = len(ports.prompts)
    with pytest.raises(LoopReceiptExistsError):
        HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert len(ports.prompts) == turns_before
    assert len(ports.receipts) == 1


def test_transcript_records_every_action_as_a_tool_call() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a("view", path="src/m.py"),
                _a("grep", pattern="add", path="src"),
                _a("finish", summary="s"),
            )
        ],
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    calls = ports.transcript["calls"]
    assert [c["tool_name"] for c in calls] == ["view", "grep", "finish"]  # type: ignore[index,union-attr]
    assert ports.transcript["execution_results"] == [["tests/test_m.py", True]]
    assert ports.transcript["answer_text"] == "s"


def test_first_turn_prompt_shows_the_context_files_and_rails() -> None:
    ports = FakePorts([], check_passes=[True])
    HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, context_paths=("src/m.py",))
    )
    prompt = ports.prompts[0]
    assert "FILE src/m.py\ndef add(a, b):" in prompt
    assert "WRITABLE (only these paths may be written): src/*.py" in prompt
    assert "- tests: pytest tests/test_m.py" in prompt


def test_path_rules() -> None:
    assert normalise_path("src/./m.py") == "src/m.py"
    assert normalise_path("../x") is None
    assert normalise_path("/abs") is None
    assert normalise_path("a/../../x") is None
    request = _request()
    assert writable(request, "src/m.py")
    assert not writable(request, ".git/config")
    assert not writable(request, ".")


def test_request_refuses_relative_root_and_escaping_globs() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        _request(workspace_root="relative/path")
    with pytest.raises(ValueError, match="not a worktree-relative path"):
        _request(writable_globs=("../*.py",))
    with pytest.raises(ValueError, match="at least 1 item"):
        _request(checks=())


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("src/*.py", "src/m.py", True),
        ("src/*.py", "src/sub/m.py", False),
        ("src/**/*.py", "src/m.py", True),
        ("src/**/*.py", "src/a/b/m.py", True),
        ("src/**", "src/a/b.txt", True),
        ("tests/test_?.py", "tests/test_a.py", True),
        ("tests/test_?.py", "tests/test_ab.py", False),
    ],
)
def test_glob_semantics(glob: str, path: str, expected: bool) -> None:
    assert writable(_request(writable_globs=(glob,)), path) is expected


def test_file_index_lists_files_near_the_task_first() -> None:
    from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.handler_delegated_code_edit_orchestrator import (
        relevant_first,
    )

    request = _request(
        writable_globs=("src/pkg/validators/*.yaml", "tests/unit/pkg/test_x.py"),
        context_paths=("src/pkg/models/m.py",),
    )
    paths = [
        "a.md",
        "docs/x.md",
        "src/pkg/models/m.py",
        "src/pkg/validators/b.yaml",
        "tests/unit/pkg/test_x.py",
        "z.txt",
    ]
    assert relevant_first(request, paths) == [
        "src/pkg/models/m.py",
        "src/pkg/validators/b.yaml",
        "tests/unit/pkg/test_x.py",
        "a.md",
        "docs/x.md",
        "z.txt",
    ]


def test_view_pages_a_long_file_and_says_how_to_see_the_rest() -> None:
    body = "".join(f"line {n}\n" for n in range(1, 601))
    ports = FakePorts(
        [
            _reply(1, _a("view", path="src/m.py")),
            _reply(2, _a("view", path="src/m.py", offset=251)),
        ],
        files={"src/m.py": body},
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))
    # What the model saw: turn 2's prompt carries turn 1's view in full.
    seen = ports.prompts[1]
    assert "[src/m.py lines 1-250 of 600]" in seen
    assert "  250| line 250\n[more: view src/m.py with offset=251]" in seen
    assert "  251| line 251" not in seen
    turns = ports.receipts[next(iter(ports.receipts))]["turns"]
    second = turns[1]["actions"][0]["output"]  # type: ignore[index]
    assert second.startswith("[src/m.py lines 251-500 of 600]")


def test_edit_recovers_from_copied_line_number_prefixes() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="    2|     return 0",
                    new_string="    2|     return a + b",
                ),
            )
        ],
        check_passes=[True],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == "def add(a, b):\n    return a + b\n"
    assert result.status == EnumCodeEditStatus.ACCEPTED


def test_failed_edit_points_at_where_the_first_line_is() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="def add(a, b):\n    return 1",
                    new_string="x",
                ),
            )
        ],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    output = ports.receipts[next(iter(ports.receipts))]["turns"][0]["actions"][0][
        "output"
    ]  # type: ignore[index]
    assert "occurs 0 times" in output
    assert "line(s) 1" in output


def test_offset_must_be_a_positive_integer() -> None:
    from omnimarket.nodes.node_delegated_code_edit_orchestrator import parse_turn_reply

    actions, reason = parse_turn_reply(
        '{"actions": [{"tool": "view", "path": "a.py", "offset": "40"}]}'
    )
    assert reason == ""
    assert actions[0].offset == 40
    _, reason = parse_turn_reply(
        '{"actions": [{"tool": "view", "path": "a.py", "offset": 0}]}'
    )
    assert "offset must be an integer" in reason


def test_a_turn_whose_results_exceed_the_history_budget_is_cut_not_dropped() -> None:
    from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
        build_turn_prompt,
    )

    request = _request()
    huge = "TURN 2\n> view(path='src/m.py') -> ok\n" + "x" * 50_000 + "\n"
    history = ["TURN 1\n> ls() -> ok\nsrc\n", huge]
    head_only = build_turn_prompt(request, ["src/m.py"], (), [], 3, max_chars=1)
    prompt = build_turn_prompt(
        request, ["src/m.py"], (), history, 3, max_chars=len(head_only) + 5_000
    )
    assert "TURN 2\n> view(path='src/m.py') -> ok" in prompt
    assert "more characters cut" in prompt
    assert len(prompt) <= len(head_only) + 5_000 + 200


def test_format_tool_runs_the_declared_formatter_over_a_writable_file() -> None:
    ports = FakePorts(
        [_reply(1, _a("format", file_path="src/m.py"), _a("finish", summary="s"))],
        check_passes=[True, True],
    )
    request = _request(formatter=("ruff", "format"))
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert ports.formatter_argv == [("ruff", "format", "src/m.py")]
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.refusals == 0


def test_format_tool_is_refused_outside_writable_globs_and_without_a_formatter() -> (
    None
):
    ports = FakePorts(
        [
            _reply(
                1,
                _a("format", file_path="tests/test_m.py"),
                _a("format", file_path="src/m.py"),
            )
        ],
        check_passes=[False],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, formatter=("ruff", "format"))
    )
    assert result.refusals == 1
    ports = FakePorts([_reply(1, _a("format", file_path="src/m.py"))])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert result.refusals == 1
    assert ports.formatter_argv == []


def test_format_is_offered_as_a_tool() -> None:
    from omnimarket.nodes.node_delegated_code_edit_orchestrator import TOOL_SCHEMAS

    schemas = cast("list[dict[str, dict[str, str]]]", list(TOOL_SCHEMAS))
    names = [t["function"]["name"] for t in schemas]
    assert "format" in names
    contract = cast("dict[str, Any]", RESPONSE_CONTRACT)
    tool_enum = contract["properties"]["actions"]["items"]["properties"]["tool"]["enum"]
    assert "format" in tool_enum


def _interrupted_ports() -> FakePorts:
    dead = ModelTurnReply(run_id="dead", ok=False, invalid_reason="delegate exited 1")
    return FakePorts(
        [
            _reply(1, _a("view", path="src/m.py")).model_copy(
                update={"tokens_in": 11, "tokens_out": 3, "model": "test-model"}
            ),
            _reply(2, _a("write", file_path="src/m.py", content=FIX)),
            dead,
            dead,
        ]
    )


def test_delegate_failed_receipt_preserves_last_good_history() -> None:
    ports = _interrupted_ports()
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    receipt = ports.receipts[request.correlation_id]
    block = cast(dict[str, Any], receipt["resume"])
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert result.resumable
    assert block["resumable"] is True
    assert block["last_good_turn"] == 2
    assert block["turns_used"] == 4
    assert block["max_turns"] == request.max_turns
    assert block["reason"] == "resumable from turn 3 (4 turns left)"
    assert len(block["history"]) == 2
    assert block["history"][0].startswith("TURN 1\n")
    assert block["history"][1].startswith("TURN 2\n")
    assert "delegate run failed" not in "".join(block["history"])
    assert (
        block["diff_sha256"] == hashlib.sha256(ports.diff(request).encode()).hexdigest()
    )
    assert block["command"] == (
        f"onex code-edit run --resume {request.correlation_id} "
        "--state-root /state, with the same delegate flags"
    )
    assert receipt["superseded_turns"] == []
    assert receipt["resumes"] == 0
    turns = cast(list[dict[str, Any]], receipt["turns"])
    assert [t["turn"] for t in turns] == [1, 2, 3, 4]
    assert [t["ok"] for t in turns] == [True, True, False, False]
    assert turns[0]["tokens_in"] == 11
    assert turns[0]["tokens_out"] == 3


def test_resume_restores_history_and_archives_failed_attempts() -> None:
    ports = _interrupted_ports()
    request = _request()
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    handler.run(request)
    prior = ports.receipts[request.correlation_id]
    history = cast(dict[str, Any], prior["resume"])["history"]
    ports.turns = [_reply(3, _a("finish", summary="fixed"))]
    ports.check_passes = [True]
    result = handler.run(request, resume=True)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.turns == 3
    assert result.local_tokens_in == 11
    assert result.local_tokens_out == 3
    assert ports.delegate_turns[-1] == 3
    assert all(block in ports.prompts[-1] for block in history)
    receipt = ports.receipts[request.correlation_id]
    assert receipt["resumes"] == 1
    assert [t["turn"] for t in cast(list[dict[str, Any]], receipt["turns"])] == [
        1,
        2,
        3,
    ]
    assert [
        t["turn"] for t in cast(list[dict[str, Any]], receipt["superseded_turns"])
    ] == [3, 4]
    assert ports.archived == [prior]
    assert cast(dict[str, Any], receipt["resume"])["history"] == []
    assert not result.resumable
    calls = cast(list[dict[str, Any]], ports.transcript["calls"])
    assert [c["call_id"] for c in calls] == ["t1a1", "t2a1", "t3a1"]
    assert json.loads(calls[1]["arguments_json"]) == {"path": "src/m.py"}


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing", "no readable receipt"),
        ("accepted", "status accepted.*1 turns used of 6"),
        ("old", "predates resume support"),
        ("diff", "worktree diff changed"),
        ("request", "request differs"),
        ("budget", "status budget_exhausted.*2 turns used of 2"),
        ("no_progress", "status no_progress.*2 turns used of 6"),
    ],
)
def test_resume_refusals_do_not_claim_or_write(case: str, reason: str) -> None:
    ports = _interrupted_ports()
    request = _request()
    if case == "accepted":
        ports.turns = [_reply(1, _a("finish"))]
        ports.check_passes = [True]
    elif case == "budget":
        request = _request(max_turns=2)
    elif case == "no_progress":
        ports.turns = [_reply(1, _a("finish")), _reply(2, _a("finish"))]
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    if case != "missing":
        handler.run(request)
    if case == "old":
        del ports.receipts[request.correlation_id]["resume"]
    elif case == "diff":
        ports.write_file(request, "src/new.py", "changed\n")
    elif case == "request":
        request = request.model_copy(update={"task": "different task"})
    before = dict(ports.receipts)
    prompts = list(ports.prompts)
    with pytest.raises(ResumeRefusedError, match=reason):
        handler.run(request, resume=True)
    assert not ports.claimed
    assert ports.receipts == before
    assert ports.archived == []
    assert ports.prompts == prompts


def test_budget_exhausted_resume_reason_names_turn_count() -> None:
    ports = FakePorts([])
    request = _request(max_turns=2)
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    block = cast(dict[str, Any], ports.receipts[request.correlation_id]["resume"])
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert not result.resumable
    assert block["resumable"] is False
    assert "2 turns used of 2" in block["reason"]
    assert block["last_good_turn"] == 2
    assert block["history"] == []
    assert block["command"] == ""


def test_resume_turn_cap_spans_segments() -> None:
    dead = ModelTurnReply(run_id="dead", ok=False)
    ports = FakePorts([_reply(1, _a("view", path="src/m.py")), dead, dead])
    request = _request(max_turns=3)
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    assert handler.run(request).status == EnumCodeEditStatus.DELEGATE_FAILED
    ports.turns = [_reply(2, _a("ls")), _reply(3, _a("ls"))]
    result = handler.run(request, resume=True)
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert ports.delegate_turns == [1, 2, 3, 2, 3]
    assert result.turns == 3


def test_unusable_received_reply_is_a_good_resume_turn() -> None:
    bad = ModelTurnReply(
        run_id="bad", ok=False, raw_text="not json", invalid_reason="not JSON"
    )
    dead = ModelTurnReply(run_id="dead", ok=False)
    ports = FakePorts([bad, dead, dead])
    request = _request()
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    block = cast(dict[str, Any], ports.receipts[request.correlation_id]["resume"])
    assert block["last_good_turn"] == 1
    assert len(block["history"]) == 1
    assert "your reply was unusable" in block["history"][0]


def test_resume_history_keeps_newest_whole_blocks_within_budget() -> None:
    actions = tuple(_a("view", path="src/m.py") for _ in range(4))
    dead = ModelTurnReply(run_id="dead", ok=False)
    ports = FakePorts(
        [_reply(n, *actions) for n in range(1, 5)] + [dead, dead],
        files={"src/m.py": "x" * 10_000},
    )
    request = _request(max_turns=10)
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    block = cast(dict[str, Any], ports.receipts[request.correlation_id]["resume"])
    assert block["last_good_turn"] == 4
    assert sum(map(len, block["history"])) <= 120_000
    assert len(block["history"]) == 2
    assert block["history"][0].startswith("TURN 3\n")
    assert block["history"][1].startswith("TURN 4\n")


def test_repeated_resumes_carry_attempts_and_preserve_check_state() -> None:
    dead = ModelTurnReply(run_id="dead", ok=False)
    ports = FakePorts(
        [
            _reply(
                1,
                _a("write", file_path="README.md", content="refused"),
                _a("finish", summary="attempted fix"),
            ),
            dead,
            dead,
        ]
    )
    request = _request()
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    first = handler.run(request)
    prior = ports.receipts[request.correlation_id]
    prior_block = cast(dict[str, Any], prior["resume"])
    assert first.refusals == 1
    assert prior_block["last_failure_key"]
    # Older turn records may omit metrics; seeding must default them to zero.
    record = cast(list[dict[str, Any]], prior["turns"])[0]
    del record["tokens_in"]
    del record["tokens_out"]
    ports.turns = [dead, dead]
    second = handler.run(request, resume=True)
    second_receipt = ports.receipts[request.correlation_id]
    second_block = cast(dict[str, Any], second_receipt["resume"])
    assert second.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert second.refusals == first.refusals
    assert second.summary == first.summary == "attempted fix"
    assert second.checks == first.checks
    assert second.local_tokens_in == second.local_tokens_out == 0
    assert second_block["history"] == prior_block["history"]
    assert second_block["last_failure_key"] == prior_block["last_failure_key"]
    assert second_block["diff_sha256"] == ""
    assert second_block["last_good_turn"] == 1
    assert second_receipt["resumes"] == 1
    ports.turns = [_reply(2, _a("finish"))]
    third = handler.run(request, resume=True)
    receipt = ports.receipts[request.correlation_id]
    assert third.status == EnumCodeEditStatus.NO_PROGRESS
    assert third.turns == 2
    assert receipt["resumes"] == 2
    assert len(cast(list[object], receipt["superseded_turns"])) == 4
    assert ports.archived == [prior, second_receipt]
