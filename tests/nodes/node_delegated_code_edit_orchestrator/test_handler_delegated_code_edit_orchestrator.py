# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegated code edit loop, driven through fake ports (OMN-20290 AC1)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
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
    WorkspacePathError,
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
        self.writes: list[str] = []
        self.checks_run: list[str] = []
        self.formatter_argv: list[tuple[str, ...]] = []
        self.prompts: list[str] = []
        self.contracts: list[dict[str, object]] = []

    def claim_loop_receipt(self, loop_run_id: str) -> None:
        if loop_run_id in self.receipts:
            raise LoopReceiptExistsError(loop_run_id)
        self.claimed.add(loop_run_id)

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        self.receipts[loop_run_id] = payload

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

    names = [t["function"]["name"] for t in TOOL_SCHEMAS]  # type: ignore[index]
    assert "format" in names
    assert (
        "format"
        in RESPONSE_CONTRACT["properties"]["actions"]["items"]["properties"]["tool"][
            "enum"
        ]
    )  # type: ignore[index]
