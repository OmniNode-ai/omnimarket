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

import jsonschema
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
    parse_turn_reply,
    writable,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.turn_protocol import (
    TOOL_SCHEMAS,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_BULK_FILES,
    MAX_OBSERVATION_BYTES,
    MAX_WRITE_BYTES,
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


def test_prompt_names_the_writable_paths_that_do_not_exist_yet() -> None:
    """OMN-20291 replay 0192c135 (loop df4d5ca5) and 8d6e9b45 (loop 692d96fe),
    and a0cbacca before them: the FILES index is cut at 12,000 characters on a
    7,211-file worktree, so the model could not tell that the test file it was
    told to write did not exist, viewed it first, and the rubric scored that
    view as a phantom path. A literal writable path missing from the worktree
    is now named as a new file until a turn has written it."""
    ports = FakePorts(
        [
            _reply(1, _a("view", path="src/m.py")),
            _reply(2, _a("write", file_path="tests/test_new.py", content="x = 1\n")),
            _reply(3, _a("finish", summary="s")),
        ],
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(writable_globs=("src/m.py", "tests/test_new.py", "docs/*.md"))
    )
    new_line = (
        "NEW FILES (not in the worktree yet: create each with write; viewing, "
        "grepping or listing one before then fails): tests/test_new.py"
    )
    assert new_line in ports.prompts[0]
    assert new_line in ports.prompts[1]
    assert "NEW FILES" not in ports.prompts[2]


def test_prompt_has_no_new_files_line_when_every_writable_path_exists() -> None:
    ports = FakePorts([], check_passes=[True])
    HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, writable_globs=("src/m.py", "src/*.py", "src"))
    )
    assert "NEW FILES" not in ports.prompts[0]


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


@pytest.mark.parametrize("formatter", [((),), ("ruff", "format")])
def test_request_refuses_empty_formatter_steps_and_flat_argv(formatter: object) -> None:
    with pytest.raises(ValueError, match="formatter"):
        _request(formatter=formatter)


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
        HistoryAction,
        HistoryTurn,
        build_turn_prompt,
    )

    request = _request()
    history = [
        HistoryTurn(1, actions=(HistoryAction("> ls() -> ok", "src"),)),
        HistoryTurn(
            2, actions=(HistoryAction("> view(path='src/m.py') -> ok", "x" * 50_000),)
        ),
    ]
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
    request = _request(formatter=(("ruff", "format"),))
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert ports.formatter_argv == [("ruff", "format", "src/m.py")]
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.refusals == 0
    turns = cast(
        "list[dict[str, Any]]", ports.receipts[request.correlation_id]["turns"]
    )
    assert (
        turns[0]["actions"][0]["output"] == "$ ruff format src/m.py\npassed (exit 0)\n"
    )


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
        _request(max_turns=1, formatter=(("ruff", "format"),))
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
    tool_names = {
        variant["properties"]["tool"]["const"]
        for variant in contract["properties"]["actions"]["items"]["anyOf"]
    }
    assert "format" in tool_names


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            {
                "note": "n",
                "actions": [
                    {"tool": "view", "path": "a.py", "offset": 3},
                    {"tool": "grep", "pattern": "x", "path": "src"},
                    {"tool": "finish", "summary": "done"},
                ],
            },
            True,
        ),
        (
            {
                "actions": [
                    {"tool": "grep", "pattern": "x", "name": "n", "summary": "s"}
                ]
            },
            False,
        ),
        (
            {
                "actions": [
                    {
                        "tool": "edit",
                        "file_path": "a.py",
                        "old_string": "a",
                        "new_string": "b",
                        "summary": "s",
                    }
                ]
            },
            False,
        ),
        (
            {
                "actions": [
                    {
                        "tool": "replace_in_files",
                        "old_string": "a",
                        "new_string": "b",
                        "glob": "src/*.py",
                    }
                ]
            },
            True,
        ),
        (
            {
                "actions": [
                    {
                        "tool": "replace_in_files",
                        "old_string": "a",
                        "new_string": "b",
                        "glob": "src/*.py",
                        "file_paths": ["a.py"],
                    }
                ]
            },
            False,
        ),
        ({"actions": [{"tool": "view"}]}, False),
        ({"actions": [{"tool": "write", "file_path": "a.py", "content": ""}]}, True),
    ],
)
def test_response_contract_and_parser_agree(
    reply: dict[str, object], expected: bool
) -> None:
    validator = jsonschema.Draft202012Validator(RESPONSE_CONTRACT)
    assert validator.is_valid(reply) is expected
    assert (parse_turn_reply(json.dumps(reply))[1] == "") is expected


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
    assert [entry["turn"] for entry in block["history"]] == [1, 2]
    assert "delegate run failed" not in json.dumps(block["history"])
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
    assert history
    assert all(f"TURN {entry['turn']}\n" in ports.prompts[-1] for entry in history)
    assert all(
        action["header"] in ports.prompts[-1]
        for entry in history
        for action in entry["actions"]
    )
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
    # a v2 receipt restores the write's full arguments, not just its target
    assert json.loads(calls[1]["arguments_json"]) == {
        "file_path": "src/m.py",
        "content": FIX,
    }


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
    assert "your reply was unusable" in block["history"][0]["message"]


def test_resume_history_keeps_newest_whole_blocks_within_budget() -> None:
    # Each turn changes a file, so its reads are never paused, and reads its
    # bounded 30,000 characters; eight such turns exceed the 120,000 budget.
    actions = (
        _a("write", file_path="src/n.py", content="n = 1\n"),
        *(_a("view", path="src/m.py") for _ in range(4)),
    )
    dead = ModelTurnReply(run_id="dead", ok=False)
    ports = FakePorts(
        [_reply(n, *actions) for n in range(1, 9)] + [dead, dead],
        files={"src/m.py": "x" * 10_000},
    )
    request = _request(max_turns=12)
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    block = cast(dict[str, Any], ports.receipts[request.correlation_id]["resume"])
    assert block["last_good_turn"] == 8
    assert sum(len(json.dumps(entry)) for entry in block["history"]) <= 120_000
    kept = [entry["turn"] for entry in block["history"]]
    # The newest turns, whole, oldest dropped first.
    assert kept == list(range(9 - len(kept), 9))
    assert 1 <= len(kept) < 8


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


def test_replace_in_files_edits_thirty_files_in_one_action_and_turn() -> None:
    files = {f"src/m{n}.py": "old\n" for n in range(30)}
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=tuple(files),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts([_reply(1, action)], files=files, check_passes=[True])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.turns == 1
    assert ports.writes == list(files)
    assert ports.files == dict.fromkeys(files, "new\n")
    calls = cast(list[dict[str, Any]], ports.transcript["calls"])
    assert len(calls) == 1
    assert calls[0]["status"] == "ok"
    assert calls[0]["output"].startswith(
        "replace_in_files: 30 edited, 0 failed of 30\n"
    )
    assert json.loads(calls[0]["arguments_json"])["file_paths"] == list(files)


def test_replace_in_files_glob_is_a_scope_over_the_writable_manifest() -> None:
    action = _a("replace_in_files", glob="**/*.py", old_string="old", new_string="new")
    files = {
        "src/z.py": "old",
        "src/sub/a.py": "old",
        "src/a.py": "old",
        "src/none.py": "other",
        "README.md": "old",
    }
    ports = FakePorts([_reply(1, action)], files=files, check_passes=[True])
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == ["src/a.py", "src/z.py"]
    assert ports.files["src/sub/a.py"] == ports.files["README.md"] == "old"
    call = cast(list[dict[str, Any]], ports.transcript["calls"])[0]
    assert call["status"] == "ok"
    assert "2 edited, 0 failed of 3" in call["output"]
    assert (
        "1 without old_string, 1 matched outside the writable globs" in call["output"]
    )
    assert json.loads(call["arguments_json"])["glob"] == "**/*.py"


def test_replace_in_files_glob_that_edits_nothing_is_an_error() -> None:
    action = _a("replace_in_files", glob="src/*.py", old_string="old", new_string="new")
    ports = FakePorts([_reply(1, action)], files={"src/a.py": "x", "src/b.py": "y"})
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    call = cast(list[dict[str, Any]], ports.transcript["calls"])[0]
    assert call["status"] == "error"
    assert "no matched file contains old_string" in call["output"]


def test_replace_in_files_partial_failure_is_one_error_call_and_feedback() -> None:
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/a.py", "src/missing.py", "src/z.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts(
        [_reply(1, action), _reply(2, _a("finish"))],
        files={"src/a.py": "old", "src/z.py": "old"},
        check_passes=[True],
    )
    request = _request()
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert ports.writes == ["src/a.py", "src/z.py"]
    assert result.refusals == 0
    calls = cast(list[dict[str, Any]], ports.transcript["calls"])
    bulk_calls = [call for call in calls if call["tool_name"] == "replace_in_files"]
    assert len(bulk_calls) == 1
    assert bulk_calls[0]["status"] == "error"
    output = bulk_calls[0]["output"]
    assert output == (
        "replace_in_files: 2 edited, 1 failed of 3\n"
        "FAILED src/missing.py: src/missing.py is not a file\n"
        "edited src/a.py (1x)\nedited src/z.py (1x)"
    )
    assert output in ports.prompts[1]
    turns = cast(list[dict[str, Any]], ports.receipts[request.correlation_id]["turns"])
    assert turns[0]["actions"][0]["ok"] is False


def test_replace_in_files_write_error_leaves_one_file_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _a("replace_in_files", glob="src/*.py", old_string="old", new_string="new")
    ports = FakePorts(
        [_reply(1, action)],
        files={"src/a.py": "old", "src/b.py": "old", "src/c.py": "old"},
    )
    write_file = ports.write_file

    def write(request: ModelDelegatedCodeEditRequest, path: str, content: str) -> None:
        if path == "src/b.py":
            raise WorkspacePathError("write denied")
        write_file(request, path, content)

    monkeypatch.setattr(ports, "write_file", write)
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files == {"src/a.py": "new", "src/b.py": "old", "src/c.py": "new"}
    assert ports.writes == ["src/a.py", "src/c.py"]
    call = cast(list[dict[str, Any]], ports.transcript["calls"])[0]
    assert call["status"] == "error"
    assert "FAILED src/b.py: write denied" in call["output"]


@pytest.mark.parametrize(
    ("use_paths", "glob"), [(True, ""), (False, "src/*.py"), (False, "missing/*.py")]
)
def test_replace_in_files_refuses_too_many_or_zero_targets(
    use_paths: bool, glob: str
) -> None:
    files = {f"src/m{n}.py": "old" for n in range(MAX_BULK_FILES + 1)}
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=tuple(files) if use_paths else (),
        glob=glob,
        old_string="old",
        new_string="new",
    )
    ports = FakePorts(
        [_reply(1, action)],
        files=files,
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert result.refusals == 1
    assert ports.files == files
    assert ports.writes == []
    call = cast(list[dict[str, Any]], ports.transcript["calls"])[0]
    assert call["status"] == "error"
    assert "refused:" in call["output"]


def test_replace_in_files_does_not_strip_view_prefixes() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "replace_in_files",
                    glob="src/*.py",
                    old_string="  1| old",
                    new_string="  1| new",
                ),
            )
        ],
        files={"src/a.py": "old", "src/b.py": "  1| old"},
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == ["src/b.py"]
    assert ports.files == {"src/a.py": "old", "src/b.py": "  1| new"}


def test_replace_in_files_normalises_deduplicates_and_replaces_every_occurrence() -> (
    None
):
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/./m.py", "src/m.py", "src/z.py"),
        old_string="old",
    )
    ports = FakePorts(
        [_reply(1, action)], files={"src/m.py": "old old old", "src/z.py": "old"}
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == ["src/m.py", "src/z.py"]
    assert ports.files == {"src/m.py": "  ", "src/z.py": ""}
    call = cast(list[dict[str, Any]], ports.transcript["calls"])[0]
    assert call["output"] == (
        "replace_in_files: 2 edited, 0 failed of 2\n"
        "edited src/m.py (3x)\nedited src/z.py (1x)"
    )


def test_replace_in_files_feedback_keeps_failures_before_capped_successes() -> None:
    files = {f"src/{'a' * 210}{n}.py": "old" for n in range(40)}
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=(*files, "src/missing.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts(
        [_reply(1, action), _reply(2, _a("finish"))], files=files, check_passes=[True]
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    output = cast(list[dict[str, Any]], ports.transcript["calls"])[0]["output"]
    assert (
        output.splitlines()[1] == "FAILED src/missing.py: src/missing.py is not a file"
    )
    assert output.index("FAILED") < output.index("\nedited")
    assert "more characters cut" in output
    assert len(output) <= MAX_OBSERVATION_BYTES + 100
    assert output in ports.prompts[1]


def test_replace_in_files_confines_paths_and_refuses_oversized_content() -> None:
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("../outside.py", "src/absent.py", "src/big.py", "src/m.py"),
        old_string="old",
        new_string="x" * MAX_WRITE_BYTES,
    )
    files = {"src/big.py": "old old", "src/m.py": "different"}
    ports = FakePorts([_reply(1, action)], files=files)
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    assert ports.files == files
    assert result.refusals == 1
    output = cast(list[dict[str, Any]], ports.transcript["calls"])[0]["output"]
    assert "FAILED ../outside.py: leaves the worktree" in output
    assert "FAILED src/absent.py: src/absent.py is not a file" in output
    assert f"FAILED src/big.py: content over {MAX_WRITE_BYTES} bytes" in output


@pytest.mark.parametrize(
    ("arguments", "reason_fragment"),
    [
        ({"file_paths": ["src/m.py"], "glob": "src/*.py"}, "exactly one"),
        ({}, "exactly one"),
        ({"file_paths": "src/m.py"}, "non-empty list"),
        ({"file_paths": None}, "non-empty list"),
        ({"file_paths": []}, "non-empty list"),
        ({"file_paths": ["src/m.py", 1]}, "non-empty strings"),
        ({"file_paths": [""]}, "non-empty strings"),
        ({"file_paths": ["src/m.py"] * (MAX_BULK_FILES + 1)}, "at most"),
        ({"glob": "/src/*.py"}, "worktree-relative"),
        ({"glob": "src/../*.py"}, "worktree-relative"),
        ({"glob": ""}, "needs a string glob"),
        ({"glob": None}, "needs a string glob"),
        ({"glob": "src/*.py", "old_string": ""}, "needs a string old_string"),
    ],
)
def test_replace_in_files_parser_rejects_invalid_arguments(
    arguments: dict[str, object],
    reason_fragment: str,
) -> None:
    actions, reason = parse_turn_reply(
        json.dumps(
            {
                "actions": [
                    {"tool": "replace_in_files", "old_string": "old", **arguments}
                ]
            }
        )
    )
    assert actions == ()
    assert reason_fragment in reason


def test_replace_in_files_parser_preserves_the_array_and_empty_replacement() -> None:
    paths = ["src/m.py", "src/z.py"]
    actions, reason = parse_turn_reply(
        json.dumps(
            {
                "actions": [
                    {
                        "tool": "replace_in_files",
                        "file_paths": paths,
                        "old_string": "old",
                        "new_string": "",
                    }
                ]
            }
        )
    )
    assert reason == ""
    assert actions[0].file_paths == tuple(paths)
    assert actions[0].new_string == ""
    assert actions[0].tool == EnumCodeEditTool.REPLACE_IN_FILES


@pytest.mark.parametrize("argument", ["file_paths", "glob"])
@pytest.mark.parametrize(
    "tool",
    [
        tool.value
        for tool in EnumCodeEditTool
        if tool != EnumCodeEditTool.REPLACE_IN_FILES
    ],
)
def test_existing_tools_reject_bulk_arguments(tool: str, argument: str) -> None:
    _, reason = parse_turn_reply(
        json.dumps(
            {
                "actions": [
                    {
                        "tool": tool,
                        argument: ["src/m.py"]
                        if argument == "file_paths"
                        else "src/*.py",
                    }
                ]
            }
        )
    )
    assert f"takes no {argument}" in reason


def test_replace_in_files_schemas_declare_arrays_and_keep_the_turn_limit() -> None:
    schema = next(
        schema
        for schema in TOOL_SCHEMAS
        if cast(dict[str, Any], schema["function"])["name"] == "replace_in_files"
    )
    function = cast(dict[str, Any], schema["function"])
    assert function["parameters"]["properties"]["file_paths"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert function["parameters"]["required"] == ["old_string"]
    actions = cast(dict[str, Any], RESPONSE_CONTRACT["properties"])["actions"]
    assert actions["maxItems"] == 12
    variants = [
        variant
        for variant in actions["items"]["anyOf"]
        if variant["properties"]["tool"]["const"] == "replace_in_files"
    ]
    assert len(variants) == 2
    file_paths_variant, glob_variant = variants
    assert file_paths_variant["required"] == ["tool", "old_string", "file_paths"]
    assert file_paths_variant["properties"]["file_paths"] == {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": MAX_BULK_FILES,
    }
    assert "glob" not in file_paths_variant["properties"]
    assert glob_variant["required"] == ["tool", "old_string", "glob"]
    assert glob_variant["properties"]["glob"] == {"type": "string", "minLength": 1}
    assert "file_paths" not in glob_variant["properties"]


# -- OMN-20291: the replay's node failure classes ------------------------------


def _calls(ports: FakePorts) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", ports.transcript["calls"])


def test_an_absolute_path_inside_the_worktree_is_used_as_its_relative_form() -> None:
    """Replay 58744096: the loop prints check argv with the worktree's absolute
    PYTHONPATH; the model searched that path and was refused as leaving the
    worktree, and the rubric scored the absolute path a phantom path."""
    ports = FakePorts(
        [
            _reply(
                1,
                _a("view", path="/work/tree/src/m.py"),
                _a("grep", pattern="add", path="/work/tree/src"),
                _a(
                    "edit",
                    file_path="/work/tree/src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
                _a("view", path="/work/treeX/src/m.py"),
                _a("finish", summary="s"),
            )
        ],
        check_passes=[True],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == FIX
    calls = _calls(ports)
    assert [c["status"] for c in calls] == ["ok", "ok", "ok", "error", "ok"]
    assert '"path": "src/m.py"' in calls[0]["arguments_json"]
    assert '"path": "src"' in calls[1]["arguments_json"]
    assert '"file_path": "src/m.py"' in calls[2]["arguments_json"]
    # A sibling directory that merely shares the prefix still leaves the worktree.
    assert "leaves the worktree" in calls[3]["output"]
    assert result.refusals == 1


def test_normalise_path_maps_an_absolute_path_under_the_root() -> None:
    assert normalise_path("/work/tree/src/m.py", root="/work/tree") == "src/m.py"
    assert normalise_path("/work/tree", root="/work/tree") == "."
    assert normalise_path("/work/tree/../x", root="/work/tree") is None
    assert normalise_path("/tmp/x.py", root="/work/tree") is None
    assert normalise_path("/abs") is None


DOC = (
    "def f():\n"
    '    """Docs.\n'
    "\n"
    "    * Owned: a CLAIM owns a PR by its ticket\n"
    "      (OMN-1), and no CLAIM owns a PR by a dispatcher. A later TERMINAL ends it.\n"
    '    """\n'
)


def test_edit_tolerates_a_uniform_indentation_shift() -> None:
    """Replay ea3ee264: the view prints ``   96|   text`` and the model copied one
    space of the ``| `` separator into every line of old_string and new_string."""
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="       (OMN-1), and no CLAIM owns a PR by a dispatcher. A later TERMINAL",
                    new_string="       (OMN-1), and no CLAIM owns a PR by a dispatcher,\n"
                    "       nor by a shared ticket. A later TERMINAL",
                ),
            )
        ],
        files={"src/m.py": DOC},
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == DOC.replace(
        "a dispatcher. A later",
        "a dispatcher,\n      nor by a shared ticket. A later",
    )
    assert [c["status"] for c in _calls(ports)] == ["ok"]


def test_a_multi_line_edit_tolerates_a_uniform_indentation_shift() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="     * Owned: a CLAIM owns a PR by its ticket\n"
                    "       (OMN-1), and no CLAIM",
                    new_string="     * Owned: a CLAIM owns a PR by its ticket\n"
                    "       (OMN-2), and no CLAIM",
                ),
            )
        ],
        files={"src/m.py": DOC},
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == DOC.replace("(OMN-1)", "(OMN-2)")


def test_an_indentation_tolerant_match_must_still_be_unique() -> None:
    body = "def f():\n    x = 1\n\ndef g():\n        x = 1\n"
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="\tx = 1",
                    new_string="\tx = 2",
                ),
            )
        ],
        files={"src/m.py": body},
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == body
    assert _calls(ports)[0]["status"] == "error"


def test_an_edit_already_applied_is_reported_applied_not_failed() -> None:
    """Replay ea3ee264 turn 7: the model re-sent an edit it had applied in turn 3."""
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
            ),
            _reply(
                2,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
            ),
        ],
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))
    assert ports.files["src/m.py"] == FIX
    calls = _calls(ports)
    assert [c["status"] for c in calls] == ["ok", "ok"]
    assert "already" in calls[1]["output"]
    assert ports.writes == ["src/m.py"]


def test_an_edit_whose_old_and_new_string_match_says_it_changes_nothing() -> None:
    """OMN-20291 replay ab8d7ef6 (loop 1c966a4a), turns 10, 12 and 13: the model
    sent old_string == new_string while chasing a ruff import-order failure and
    was told the edit 'was applied before', so it kept re-sending it."""
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return 0",
                ),
            ),
        ],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    call = _calls(ports)[0]
    assert call["status"] == "ok"
    assert "applied before" not in call["output"]
    assert "old_string and new_string are the same" in call["output"]
    assert ports.writes == []


def test_an_insertion_already_applied_is_not_inserted_twice() -> None:
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="def add(a, b):",
                    new_string="def add(a, b):\n    # sum",
                ),
            ),
            _reply(
                2,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="def add(a, b):",
                    new_string="def add(a, b):\n    # sum",
                ),
            ),
        ],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))
    assert ports.files["src/m.py"] == "def add(a, b):\n    # sum\n    return 0\n"
    assert [c["status"] for c in _calls(ports)] == ["ok", "ok"]


def _views(n: int, path: str = "src/big.py") -> ModelTurnReply:
    return _reply(
        n, *(_a("view", path=path, offset=str(1 + 250 * k)) for k in range(4))
    )


def test_every_earlier_turn_stays_in_the_prompt_when_its_output_cannot() -> None:
    """Replays a16a3138 and e3a2c923: from turn 3 the prompt sat at its cap and
    held only the last one to three turns, so the model forgot what it had read
    and re-read the same files for 40 turns without editing."""
    big = "".join(f"line {i} " + "x" * 40 + "\n" for i in range(1, 1001))
    turns = [
        _views(1),
        _reply(2, _a("grep", pattern="line 7", path="src")),
        *(_views(n) for n in range(3, 9)),
    ]
    ports = FakePorts(turns, files={"src/m.py": "x\n", "src/big.py": big})
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=9))
    last = ports.prompts[-1]
    for n in range(1, 9):
        assert f"TURN {n}\n" in last, n
    assert "> grep(path='src', pattern='line 7') -> ok" in last
    assert "[earlier turns cut to fit]" not in last
    # The newest turn's views are shown in full; a window shown again later is
    # not repeated, it points at the later turn.
    assert last.count("line 250 ") == 1
    # Turns 4, 6 and 8 have their reads paused (three read-only turns in a
    # row), so turn 7 holds the last views.
    assert "shown again in turn 7" in last


def test_a_view_of_a_file_changed_later_is_marked_stale() -> None:
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
            ),
        ],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=3))
    last = ports.prompts[-1]
    assert "src/m.py changed in turn 2 after this view" in last


def test_one_turns_reads_are_bounded_so_the_newest_turn_is_never_cut() -> None:
    """Replays a16a3138 and e3a2c923 again: a turn read 8 to 12 windows of up to
    16 KB, more than the whole history budget, so even the newest turn was shown
    cut and the model re-read what it had just read."""
    big = "".join(f"line {i} " + "x" * 40 + "\n" for i in range(1, 1001))
    ports = FakePorts(
        [_views(1), _views(2)], files={"src/m.py": "x\n", "src/big.py": big}
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=3))
    receipt = next(iter(ports.receipts.values()))
    turns = cast("list[dict[str, Any]]", receipt["turns"])
    actions = cast("list[dict[str, Any]]", turns[0]["actions"])
    outputs = [a["output"] for a in actions]
    oks = [a["ok"] for a in actions]
    assert oks[:2] == [True, True]
    assert sum(len(o) for o, ok in zip(outputs, oks, strict=True) if ok) <= 30_000
    assert not oks[-1]
    assert "this turn's reads" in outputs[-1]
    # The newest turn, and the one before it, are shown whole.
    last = ports.prompts[-1]
    assert "more characters cut" not in last
    assert "line 250 " in last


def test_a_view_shrinks_to_what_the_turn_can_still_read() -> None:
    from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.handler_delegated_code_edit_orchestrator import (
        view_window,
    )

    text = "".join(f"row {i}\n" for i in range(1, 501))
    shown = view_window(text, "a.py", 1, limit=500)
    assert len(shown) <= 500 + 200
    assert "[more: view a.py with offset=" in shown


def test_reads_pause_after_three_turns_that_read_and_change_nothing() -> None:
    """Replays a16a3138 and e3a2c923 a third time: with the history fixed and one
    turn's reads bounded, the model still read for 30 turns without editing."""
    view = _a("view", path="src/m.py")
    edit = _a(
        "edit", file_path="src/m.py", old_string="return 0", new_string="return a + b"
    )
    ports = FakePorts(
        [
            _reply(1, view),
            _reply(2, view),
            _reply(3, view),
            _reply(4, view, edit),
            _reply(5, view),
        ],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=5))
    assert "reads are refused this turn" not in ports.prompts[2]
    assert "reads are refused this turn" in ports.prompts[3]
    receipt = next(iter(ports.receipts.values()))
    turns = cast("list[dict[str, Any]]", receipt["turns"])
    fourth = cast("list[dict[str, Any]]", turns[3]["actions"])
    assert [a["ok"] for a in fourth] == [False, True]
    assert "change no file" in fourth[0]["output"]
    # The edit ended the streak: the next turn reads again.
    fifth = cast("list[dict[str, Any]]", turns[4]["actions"])
    assert fifth[0]["ok"]
    assert ports.files["src/m.py"] == FIX


def test_a_paused_turn_that_still_changes_nothing_allows_one_more_read_turn() -> None:
    view = _a("view", path="src/m.py")
    ports = FakePorts([_reply(n, view) for n in range(1, 7)])
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=6))
    receipt = next(iter(ports.receipts.values()))
    turns = cast("list[dict[str, Any]]", receipt["turns"])
    oks = [cast("list[dict[str, Any]]", t["actions"])[0]["ok"] for t in turns]
    assert oks == [True, True, True, False, True, False]


def test_an_edit_whose_first_line_alone_carries_an_extra_blank_applies() -> None:
    """Replay ea3ee264, re-run 2: copying from ``| def f(`` the model kept the
    separator's blank on the first line only; every other line was exact."""
    body = "x = 1\n\n\ndef add(a, b):\n    return 0\n"
    ports = FakePorts(
        [
            _reply(
                1,
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string=" def add(a, b):\n    return 0",
                    new_string=" def add(a, b, c=0):\n    return a + b + c",
                ),
            )
        ],
        files={"src/m.py": body},
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.files["src/m.py"] == (
        "x = 1\n\n\ndef add(a, b, c=0):\n    return a + b + c\n"
    )
    assert [c["status"] for c in _calls(ports)] == ["ok"]


def test_replace_in_files_caps_the_files_holding_old_string_not_the_glob() -> None:
    """Replay 8e1b5f72 (OMN-20392 AC2, loop 7ea45ffe): the glob
    ``src/omnimarket/nodes/*/contract.yaml`` matches more node contracts than
    the cap, though fewer hold old_string, and was refused outright; the model
    fell back to a /tmp helper script and 117 single edits."""
    files = {f"src/m{n}.py": "other" for n in range(MAX_BULK_FILES + 50)}
    files.update({"src/m1.py": "old", "src/m7.py": "old\nold"})
    action = _a("replace_in_files", glob="src/*.py", old_string="old", new_string="new")
    ports = FakePorts([_reply(1, action)], files=files, check_passes=[True])
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert sorted(ports.writes) == ["src/m1.py", "src/m7.py"]
    assert ports.files["src/m7.py"] == "new\nnew"
    call = _calls(ports)[0]
    assert call["status"] == "ok"
    assert f"2 edited, 0 failed of {MAX_BULK_FILES + 50}" in call["output"]
    assert f"{MAX_BULK_FILES + 48} without old_string" in call["output"]


def test_the_prompt_names_the_file_list_as_the_only_scope_and_says_no_scripts() -> None:
    """Replay 8e1b5f72 and f76301f0: the task text recommends a helper script,
    the model wrote one first, the write was refused, and the refusal cost
    budget. The prompt says up front that there is no script tool."""
    ports = FakePorts([_reply(1, _a("ls"))])
    HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, file_list=("src/m.py",))
    )
    prompt = ports.prompts[0]
    assert "only edit scope" in prompt
    assert "helper script" in prompt
    assert "not available" in prompt
    assert "1 file" in prompt


def test_the_prompt_rails_hold_without_a_declared_file_list() -> None:
    ports = FakePorts([_reply(1, _a("ls"))])
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert "helper script" in ports.prompts[0]
    assert "only edit scope" in ports.prompts[0]


def test_replace_in_files_glob_only_narrows_the_declared_file_list() -> None:
    """Replay f76301f0 run 2 and 8e1b5f72 run 2: a glob over every node contract
    changed files outside the task's named list (5 and 27)."""
    files = {f"src/m{n}.py": "old" for n in range(5)}
    action = _a("replace_in_files", glob="src/*.py", old_string="old", new_string="new")
    ports = FakePorts([_reply(1, action)], files=files, check_passes=[True])
    HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, file_list=("src/m1.py", "src/m3.py"))
    )
    assert sorted(ports.writes) == ["src/m1.py", "src/m3.py"]
    assert ports.files["src/m0.py"] == ports.files["src/m4.py"] == "old"
    call = _calls(ports)[0]
    assert call["status"] == "ok"
    assert "2 edited, 0 failed of 2" in call["output"]
    assert "3 matched outside the task's file list" in call["output"]


def test_replace_in_files_names_outside_the_file_list_are_refused() -> None:
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/m1.py", "src/m2.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts(
        [_reply(1, action)],
        files={"src/m1.py": "old", "src/m2.py": "old"},
        check_passes=[True],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(
        _request(max_turns=1, file_list=("src/m1.py",))
    )
    assert ports.writes == ["src/m1.py"]
    assert ports.files["src/m2.py"] == "old"
    call = _calls(ports)[0]
    assert call["status"] == "error"
    assert "FAILED src/m2.py: not in the task's file list" in call["output"]
    assert result.refusals == 1


def test_replace_in_files_without_a_file_list_keeps_the_writable_scope() -> None:
    action = _a("replace_in_files", glob="src/*.py", old_string="old", new_string="new")
    ports = FakePorts(
        [_reply(1, action)],
        files={"src/a.py": "old", "src/b.py": "old"},
        check_passes=[True],
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert sorted(ports.writes) == ["src/a.py", "src/b.py"]
    assert "matched outside the task's file list" not in _calls(ports)[0]["output"]


def test_a_file_list_entry_must_be_a_worktree_relative_path() -> None:
    assert _request(file_list=("src/m.py",)).file_list == ("src/m.py",)
    for bad in ("/etc/passwd", "../x.py", "src/../../x.py", ""):
        with pytest.raises(ValueError, match="not a worktree-relative path"):
            _request(file_list=(bad,))


def test_replace_in_files_skips_a_listed_file_without_old_string() -> None:
    """Replay 8e1b5f72 run 1 ``t4a1``: the model repeated the call over the full
    list once per variant, and every file lacking that variant's old_string
    failed the call."""
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/a.py", "src/b.py", "src/c.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts(
        [_reply(1, action)],
        files={"src/a.py": "old", "src/b.py": "different", "src/c.py": "old"},
        check_passes=[True],
    )
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == ["src/a.py", "src/c.py"]
    assert ports.files["src/b.py"] == "different"
    call = _calls(ports)[0]
    assert call["status"] == "ok"
    assert "2 edited, 0 failed of 3" in call["output"]
    assert "1 skipped without old_string" in call["output"]
    assert "src/b.py" in call["output"]
    assert result.refusals == 0


def test_replace_in_files_over_a_list_where_none_holds_old_string_is_an_error() -> None:
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/a.py", "src/b.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts([_reply(1, action)], files={"src/a.py": "x", "src/b.py": "y"})
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == []
    call = _calls(ports)[0]
    assert call["status"] == "error"
    assert "no listed file contains old_string" in call["output"]


def test_replace_in_files_still_fails_a_listed_file_that_is_missing() -> None:
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/a.py", "src/gone.py"),
        old_string="old",
        new_string="new",
    )
    ports = FakePorts([_reply(1, action)], files={"src/a.py": "old"})
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    call = _calls(ports)[0]
    assert call["status"] == "error"
    assert "FAILED src/gone.py" in call["output"]
    assert "1 edited, 1 failed of 2" in call["output"]


def test_refused_calls_are_marked_so_the_budget_counts_them_separately() -> None:
    """Replay 8e1b5f72, 4 refused writes and 7 refused reads: a call the loop
    refused changed and showed nothing; it is recorded and marked, never
    counted as work against the tool-call budget."""
    view = _a("view", path="src/m.py")
    ports = FakePorts(
        [
            _reply(
                1,
                _a("write", file_path="/tmp/helper.py", content="x"),
                _a("write", file_path="scripts/helper.py", content="x"),
                _a("edit", file_path="src/m.py", old_string="nope", new_string="x"),
            ),
            _reply(2, view),
            _reply(3, view),
            _reply(4, view),
            _reply(5, view),
        ]
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=5))
    by_id = {c["call_id"]: c for c in _calls(ports)}
    assert by_id["t1a1"]["refused"] is True
    assert by_id["t1a2"]["refused"] is True
    # An edit that ran and failed is work, not a refusal.
    assert by_id["t1a3"]["status"] == "error"
    assert by_id["t1a3"]["refused"] is False
    # Turns 2 to 4 read and change nothing, so turn 5's view is refused.
    assert by_id["t5a1"]["refused"] is True
    assert by_id["t4a1"]["refused"] is False


def test_replace_in_files_does_not_apply_an_insertion_twice() -> None:
    """Replay 8e1b5f72 (attempt 2 of the lab re-run): the model sent the same
    insertion over the whole list in turns 1, 3, 9, 11 and 12, and each repeat
    inserted the line again in files that already had it; 31 turns went on
    cleaning up. A file whose old_string occurs only inside an earlier result
    is already done: skipped and reported, as a single edit reports it."""
    action = ModelCodeEditAction(
        tool=EnumCodeEditTool.REPLACE_IN_FILES,
        file_paths=("src/a.py", "src/b.py"),
        old_string="node_type: X\n",
        new_string="node_type: X\nprofiles: [main]\n",
    )
    ports = FakePorts(
        [_reply(1, action), _reply(2, action)],
        files={
            "src/a.py": "node_type: X\n",
            "src/b.py": "node_type: X\nprofiles: [main]\n",
        },
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))
    assert ports.files["src/a.py"] == ports.files["src/b.py"]
    assert ports.files["src/a.py"] == "node_type: X\nprofiles: [main]\n"
    first, second = _calls(ports)
    assert first["status"] == "ok"
    assert "1 edited, 0 failed of 2" in first["output"]
    assert "skipped (already applied): src/b.py" in first["output"]
    # The repeat changes nothing, and is reported applied, not failed.
    assert second["status"] == "ok"
    assert second["output"].startswith("unchanged replace_in_files: 0 edited")
    assert ports.writes == ["src/a.py"]


def test_replace_in_files_glob_counts_files_already_done() -> None:
    action = _a(
        "replace_in_files",
        glob="src/*.py",
        old_string="node_type: X\n",
        new_string="node_type: X\nprofiles: [main]\n",
    )
    ports = FakePorts(
        [_reply(1, action)],
        files={
            "src/a.py": "node_type: X\n",
            "src/b.py": "node_type: X\nprofiles: [main]\n",
            "src/c.py": "other\n",
        },
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=1))
    assert ports.writes == ["src/a.py"]
    call = _calls(ports)[0]
    assert call["status"] == "ok"
    assert "1 without old_string" in call["output"]
    assert "1 already applied" in call["output"]
