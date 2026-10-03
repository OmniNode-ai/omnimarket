# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real code edit ports over a temporary git worktree (OMN-20290)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.delegated_code_edit.loop_ports import (
    DelegatedCodeEditPorts,
    check_fingerprint,
    deployed_lane_delegate_flags,
    score_transcript,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    RESPONSE_CONTRACT,
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    LoopReceiptExistsError,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ResumeRefusedError,
    WorkspacePathError,
)

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "m.py").write_text("def add(a, b):\n    return 0\n")
    (root / "tests" / "test_m.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom m import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n"
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _request(root: Path, **kw: object) -> ModelDelegatedCodeEditRequest:
    fields: dict[str, object] = {
        "correlation_id": str(uuid.uuid4()),
        "task": "make add return the sum",
        "workspace_root": str(root),
        "writable_globs": ("src/*.py",),
        "checks": (
            ModelDeclaredCheck(
                name="tests",
                argv=(
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_m.py",
                ),
                targets=("tests/test_m.py",),
            ),
        ),
        "max_turns": 3,
        "caller_lane": "lane-x",
        "ticket": "OMN-20290",
    }
    fields.update(kw)
    return ModelDelegatedCodeEditRequest.model_validate(fields)


def _ports(tmp_path: Path, runner: object = None) -> DelegatedCodeEditPorts:
    return DelegatedCodeEditPorts(
        onex=Path("/bin/onex"),
        state_root=tmp_path / "state",
        delegate_flags=deployed_lane_delegate_flags("dev"),
        run_delegate=runner,  # type: ignore[arg-type]
    )


def test_reads_and_writes_stay_inside_the_worktree(tree: Path, tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    request = _request(tree)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    (tree / "src" / "link.py").symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        ports.read_file(request, "src/link.py")
    with pytest.raises(WorkspacePathError):
        ports.write_file(request, "src/link.py", "x")
    with pytest.raises(WorkspacePathError):
        ports.read_file(request, "../secret.txt")
    with pytest.raises(WorkspacePathError):
        ports.read_file(request, ".git/config")
    assert outside.read_text() == "secret\n"
    ports.write_file(request, "src/new/n.py", "n = 1\n")
    assert (tree / "src" / "new" / "n.py").read_text() == "n = 1\n"


def test_workspace_files_lists_tracked_and_untracked_with_line_counts(
    tree: Path, tmp_path: Path
) -> None:
    (tree / "src" / "u.py").write_text("a\nb\n")
    files = dict(_ports(tmp_path).workspace_files(_request(tree)))
    assert files["src/m.py"] == 2
    assert files["src/u.py"] == 2
    assert files["tests/test_m.py"] == 6


def test_grep_and_ls(tree: Path, tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    request = _request(tree)
    assert "src/m.py:1:def add(a, b):" in ports.grep(request, "def add", ".")
    assert ports.grep(request, "nothing_here_zz", ".") == "no matches"
    assert ports.list_dir(request, ".").splitlines() == ["src/", "tests/"]


def test_grep_of_a_path_that_does_not_exist_says_so(tree: Path, tmp_path: Path) -> None:
    """OMN-20291 replay ab8d7ef6 (loops 1c966a4a and f7c345be on omnimarket
    dev 32b6f90ce): the model grepped 'omnimarket/nodes/...' in a src-layout
    repo and was told 'no matches' eight times, so it never learned the path
    was wrong and never read the contract its tests assert on. view and ls
    already say a missing path is missing; grep now does too."""
    ports = _ports(tmp_path)
    request = _request(tree)
    with pytest.raises(WorkspacePathError, match="m/nodes does not exist"):
        ports.grep(request, "def add", "m/nodes")
    assert "src/m.py:1:def add(a, b):" in ports.grep(request, "def add", "src")
    assert ports.grep(request, "def add", "src/m.py").startswith("src/m.py:1:")


def test_diff_includes_untracked_files_and_leaves_the_index_alone(
    tree: Path, tmp_path: Path
) -> None:
    ports = _ports(tmp_path)
    request = _request(tree)
    ports.write_file(request, "src/m.py", "def add(a, b):\n    return a + b\n")
    ports.write_file(request, "src/extra.py", "x = 1\n")
    diff = ports.diff(request)
    assert "+++ b/src/m.py" in diff
    assert "+++ b/src/extra.py" in diff
    staged = subprocess.run(
        ["git", "-C", str(tree), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout
    assert staged == ""


def test_run_check_reports_pass_fail_and_a_stable_fingerprint(
    tree: Path, tmp_path: Path
) -> None:
    ports = _ports(tmp_path)
    request = _request(tree)
    failed = ports.run_check(request, request.checks[0])
    assert failed.status == "failed"
    again = ports.run_check(request, request.checks[0])
    assert failed.fingerprint == again.fingerprint
    ports.write_file(request, "src/m.py", "def add(a, b):\n    return a + b\n")
    assert ports.run_check(request, request.checks[0]).status == "passed"
    missing = ModelDeclaredCheck(name="nope", argv=("/no/such/binary",))
    assert ports.run_check(request, missing).status == "infra_error"


def test_fingerprint_ignores_timings() -> None:
    assert check_fingerprint(1, "1 failed in 0.52s") == check_fingerprint(
        1, "1 failed in 3.10s"
    )
    assert check_fingerprint(1, "1 failed") != check_fingerprint(2, "1 failed")


def test_delegate_argv_carries_the_contract_lane_ticket_and_deployed_flags(
    tree: Path, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.txt").write_text(
            'prose first\n```json\n{"actions": [{"tool": "view", "path": "src/m.py"}]}\n```'
        )
        (run_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "model": "Qwen3.8-27B",
                    "receipt": {
                        "result": {
                            "terminal_payload": {
                                "payload": {
                                    "metrics": {"input_tokens": 10, "output_tokens": 4}
                                }
                            }
                        }
                    },
                }
            )
        )
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"run_id": run_id}) + "\n", ""
        )

    ports = _ports(tmp_path, runner)
    reply = ports.delegate(_request(tree), "PROMPT", RESPONSE_CONTRACT, 1)
    argv = seen[0]
    assert argv[:3] == ["/bin/onex", "delegate", "PROMPT"]
    assert argv[argv.index("--task-type") + 1] == "code_generation"
    assert json.loads(argv[argv.index("--response-contract") + 1]) == RESPONSE_CONTRACT
    assert argv[argv.index("--caller-lane") + 1] == "lane-x"
    assert argv[argv.index("--ticket") + 1] == "OMN-20290"
    assert argv[argv.index("--locus") + 1] == "deployed-lane"
    assert reply.ok
    assert reply.run_id == run_id
    assert reply.model == "Qwen3.8-27B"
    assert reply.tokens_in == 10
    assert reply.tokens_out == 4
    assert reply.actions[0].path == "src/m.py"


def test_delegate_failure_is_a_failed_reply_not_an_exception(
    tree: Path, tmp_path: Path
) -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 3, "", "TRANSPORT FAILURE")

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert not reply.raw_text
    assert "exited 3" in reply.invalid_reason


@pytest.mark.parametrize("has_receipt", [False, True])
def test_delegate_failure_keeps_the_head_and_tail_of_long_stderr(
    tree: Path, tmp_path: Path, has_receipt: bool
) -> None:
    head, tail = "HEADMARK", 'UndefinedTable: relation "x" does not exist'
    stderr = head + "x" * (5000 - len(head) - len(tail)) + tail
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if has_receipt:
            run_dir = tmp_path / "state" / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "receipt.json").write_text(
                json.dumps({"failure_reason": "receipt failure"})
            )
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"run_id": run_id}), stderr + " \n"
        )

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert len(reply.invalid_reason) <= 4096
    assert reply.invalid_reason.startswith("onex delegate exited 1: HEADMARK")
    assert tail in reply.invalid_reason
    suffix = " | receipt: receipt failure" if has_receipt else ""
    assert reply.invalid_reason.endswith(tail + suffix)


def _failed_receipt(run_id: str) -> dict[str, object]:
    return {
        "status": "failed",
        "model": "",
        "run_id": run_id,
        "failure_reason": "delegate workflow failed",
        "terminal_failure_cause": "runtime error",
        "terminal_failure_reason": None,
        "receipt": {
            "status": "failed",
            "exit_code": 1,
            "metrics": {},
            "result": {
                "error": "",
                "exit_code": 1,
                "runtime_error_type": "UndefinedTable",
                "runtime_error_is_transport": False,
                "workflow_result": "failed",
                "terminal_payload": None,
                "handler_result": None,
            },
        },
    }


def test_a_null_terminal_payload_is_a_failed_reply_not_an_exception(
    tree: Path, tmp_path: Path
) -> None:
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(json.dumps(_failed_receipt(run_id)))
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"run_id": run_id}) + "\n", "delegate failed"
        )

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert reply.raw_text == ""
    assert reply.run_id == run_id
    assert "exited 1" in reply.invalid_reason
    assert "UndefinedTable" in reply.invalid_reason
    assert "terminal_payload is null" in reply.invalid_reason
    assert "delegate workflow failed" in reply.invalid_reason


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize(
    "receipt_text",
    [
        "null",
        "[]",
        '"receipt"',
        '{"receipt": null}',
        '{"receipt": "invalid"}',
        '{"receipt": {"result": null}}',
        '{"receipt": {"result": []}}',
        '{"receipt": {"result": {"terminal_payload": null}}}',
        '{"receipt": {"result": {"terminal_payload": "invalid"}}}',
        '{"receipt": {"result": {"terminal_payload": {"payload": null}}}}',
        '{"receipt": {"result": {"terminal_payload": {"payload": "invalid"}}}}',
        '{"receipt": {"result": {"metrics": null}}}',
        '{"receipt": {"result": {"metrics": []}}}',
        '{"receipt": {"result": {"metrics": {"input_tokens": "x", "output_tokens": "x"}}}}',
        '{"receipt": {"result": {"metrics": {"input_tokens": [], "output_tokens": {}}}}}',
        "not json",
    ],
)
def test_malformed_receipts_return_failed_replies(
    tree: Path, tmp_path: Path, receipt_text: str, returncode: int
) -> None:
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(receipt_text)
        return subprocess.CompletedProcess(
            argv, returncode, json.dumps({"run_id": run_id}), "stderr\n" + "x" * 310
        )

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert reply.raw_text == ""
    assert reply.run_id == run_id
    assert (reply.tokens_in, reply.tokens_out) == (0, 0)
    reason = (
        "onex delegate exited 1: stderr\n" + "x" * 310
        if returncode
        else f"onex delegate run {run_id} returned no result text"
    )
    assert reply.invalid_reason.startswith(reason)


@pytest.mark.parametrize(
    ("result_block", "expected_tokens"),
    [
        (
            {
                "terminal_payload": {
                    "payload": {"metrics": {"input_tokens": 10, "output_tokens": 4}}
                }
            },
            (10, 4),
        ),
        ({"metrics": {"input_tokens": 7, "output_tokens": 3}}, (7, 3)),
    ],
    ids=["deployed-lane", "in-process"],
)
@pytest.mark.parametrize("has_text", [False, True])
@pytest.mark.parametrize("returncode", [0, 1])
def test_delegate_receipt_metrics_are_preserved(
    tree: Path,
    tmp_path: Path,
    result_block: dict[str, object],
    expected_tokens: tuple[int, int],
    has_text: bool,
    returncode: int,
) -> None:
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "model": "test-model",
                    "receipt": {"result": result_block},
                }
            )
        )
        if has_text:
            (run_dir / "result.txt").write_text('{"actions": [{"tool": "ls"}]}')
        return subprocess.CompletedProcess(
            argv, returncode, json.dumps({"run_id": run_id}), "stderr"
        )

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert reply.ok is has_text
    assert (reply.tokens_in, reply.tokens_out) == expected_tokens
    assert reply.model == "test-model"
    if not has_text and returncode == 0:
        assert (
            reply.invalid_reason
            == f"onex delegate run {run_id} returned no result text"
        )
    elif not has_text:
        assert reply.invalid_reason == "onex delegate exited 1: stderr"


def test_delegate_failure_details_are_combined_on_one_line(
    tree: Path, tmp_path: Path
) -> None:
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "terminal_failure_cause": "runtime failure",
                    "terminal_failure_reason": "missing\ntable",
                    "failure_reason": "delegate\r\nfailed",
                    "receipt": {
                        "result": {
                            "error": "query\nfailed",
                            "runtime_error_type": "UndefinedTable",
                            "terminal_payload": None,
                        }
                    },
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert reply.invalid_reason == (
        f"onex delegate run {run_id} returned no result text"
        " | receipt: failed; runtime failure; missing table; delegate failed; "
        "query failed; UndefinedTable; terminal_payload is null"
    )


def test_delegate_with_exit_zero_and_no_text_bounds_long_receipt_errors(
    tree: Path, tmp_path: Path
) -> None:
    run_id = str(uuid.uuid4())

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(
            json.dumps({"failure_reason": "HEADMARK" + "x" * 5000 + "UndefinedTable"})
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert len(reply.invalid_reason) <= 4096
    assert reply.invalid_reason.startswith(
        f"onex delegate run {run_id} returned no result text | receipt: HEADMARK"
    )
    assert reply.invalid_reason.endswith("UndefinedTable")


def test_unreadable_delegate_receipt_returns_a_failed_reply(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = str(uuid.uuid4())
    read_text = Path.read_text

    def unreadable(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path.name == "receipt.json":
            raise PermissionError("unreadable")
        return read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", unreadable)

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    reply = _ports(tmp_path, runner).delegate(_request(tree), "P", RESPONSE_CONTRACT, 1)
    assert not reply.ok
    assert reply.raw_text == ""


def test_a_loop_whose_delegate_receipts_are_null_ends_delegate_failed_with_a_receipt_and_no_claim(
    tree: Path, tmp_path: Path
) -> None:
    ids: list[str] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_id = str(uuid.uuid4())
        ids.append(run_id)
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text(json.dumps(_failed_receipt(run_id)))
        return subprocess.CompletedProcess(argv, 1, json.dumps({"run_id": run_id}), "")

    ports = _ports(tmp_path, runner)
    request = _request(tree)
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert result.delegate_run_ids == tuple(ids)
    assert len(ids) == 2
    loop_dir = tmp_path / "state" / "runs" / request.correlation_id
    assert (loop_dir / "loop_receipt.json").is_file()
    receipt = json.loads((loop_dir / "loop_receipt.json").read_text())
    assert "UndefinedTable" in receipt["error"]["detail"]
    assert not (loop_dir / "loop_claim").exists()
    with pytest.raises(
        LoopReceiptExistsError, match="already claimed and has a receipt"
    ):
        HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert len(ids) == 2


def test_failed_loop_receipt_keeps_the_error_at_the_end_of_long_stderr(
    tree: Path, tmp_path: Path
) -> None:
    head, tail = "HEADMARK", 'UndefinedTable: relation "x" does not exist'
    stderr = head + "x" * (5000 - len(head) - len(tail)) + tail

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_id = str(uuid.uuid4())
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "receipt.json").write_text("null")
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"run_id": run_id}), stderr
        )

    request = _request(tree)
    result = HandlerDelegatedCodeEditOrchestrator(_ports(tmp_path, runner)).run(request)
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert len(result.detail) <= 300
    assert tail in result.detail
    loop_dir = tmp_path / "state" / "runs" / request.correlation_id
    receipt = json.loads((loop_dir / "loop_receipt.json").read_text())
    assert receipt["error"]["status"] == "delegate_failed"
    assert receipt["error"]["turns"] == 2
    assert len(receipt["error"]["detail"]) <= 4096
    assert head in receipt["error"]["detail"]
    assert tail in receipt["error"]["detail"]
    for turn in receipt["turns"]:
        assert len(turn["invalid_reason"]) <= 4096
        assert head in turn["invalid_reason"]
        assert tail in turn["invalid_reason"]
    assert not (loop_dir / "loop_claim").exists()


def test_an_unexpected_port_exception_still_writes_the_receipt_and_releases_the_claim(
    tree: Path, tmp_path: Path
) -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("boom")

    request = _request(tree)
    result = HandlerDelegatedCodeEditOrchestrator(_ports(tmp_path, runner)).run(request)
    assert result.status == EnumCodeEditStatus.INFRA_ERROR
    assert "RuntimeError: boom" in result.detail
    loop_dir = tmp_path / "state" / "runs" / request.correlation_id
    assert (loop_dir / "loop_receipt.json").is_file()
    assert not (loop_dir / "loop_claim").exists()


def test_end_to_end_loop_with_a_scripted_model_is_accepted_and_scored(
    tree: Path, tmp_path: Path
) -> None:
    replies = [
        {"actions": [{"tool": "view", "path": "src/m.py"}]},
        {
            "actions": [
                {
                    "tool": "edit",
                    "file_path": "src/m.py",
                    "old_string": "return 0",
                    "new_string": "return a + b",
                },
                {"tool": "run_check", "name": "tests"},
            ]
        },
        {
            "actions": [
                {
                    "tool": "finish",
                    "summary": "add returns a + b; tests/test_m.py passes",
                }
            ]
        },
    ]
    ids: list[str] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        run_id = str(uuid.uuid4())
        ids.append(run_id)
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text(json.dumps(replies[len(ids) - 1]))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    ports = _ports(tmp_path, runner)
    request = _request(tree)
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.delegate_run_ids == tuple(ids)
    assert result.changed_paths == ("src/m.py",)
    assert result.rubric_outcome == "PASS", receipt_text(tmp_path, request)
    receipt = json.loads(
        (
            tmp_path / "state" / "runs" / request.correlation_id / "loop_receipt.json"
        ).read_text()
    )
    assert receipt["delegate_run_ids"] == ids
    assert receipt["rubric_verdict"]["schema"] == "tool-use-rubric-verdict.v1"
    assert "+    return a + b" in receipt["diff"]
    with pytest.raises(LoopReceiptExistsError):
        HandlerDelegatedCodeEditOrchestrator(ports).run(request)


def test_score_transcript_passes_a_clean_run() -> None:
    record = score_transcript(
        {
            "request_text": "fix add",
            "answer_text": "fixed; tests/test_m.py passes",
            "tool_schemas": [
                {
                    "type": "function",
                    "function": {
                        "name": "view",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_check",
                        "parameters": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                },
            ],
            "calls": [
                {
                    "call_id": "t1a1",
                    "tool_name": "view",
                    "arguments_json": '{"path": "src/m.py"}',
                    "status": "ok",
                    "output": "def add",
                },
                {
                    "call_id": "t1a2",
                    "tool_name": "run_check",
                    "arguments_json": '{"name": "tests"}',
                    "status": "ok",
                    "output": "$ pytest -q tests/test_m.py\npassed (exit 0)\n1 passed",
                },
            ],
            "turn_count": 1,
            "wall_time_ms": 1000,
            "workspace_files": [["src/m.py", 2], ["tests/test_m.py", 6]],
            "execution_results": [["tests/test_m.py", True]],
        },
        run_ref="r",
    )
    assert record["verdict"]["outcome"] == "PASS"  # type: ignore[index]


def receipt_text(tmp_path: Path, request: ModelDelegatedCodeEditRequest) -> str:
    path = tmp_path / "state" / "runs" / request.correlation_id / "loop_receipt.json"
    return json.dumps(json.loads(path.read_text())["rubric_verdict"]["verdict"])[:2000]


def test_claim_is_exclusive_even_before_a_receipt_exists(tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    with pytest.raises(LoopReceiptExistsError, match="already claimed"):
        ports.claim_loop_receipt(loop_id)


def test_claim_is_refused_after_a_receipt_releases_the_claim(tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    ports.write_loop_receipt(loop_id, {"status": "done"})
    loop_dir = tmp_path / "state" / "runs" / loop_id
    assert not (loop_dir / "loop_claim").exists()
    with pytest.raises(
        LoopReceiptExistsError, match="already claimed and has a receipt"
    ):
        ports.claim_loop_receipt(loop_id)
    assert not (loop_dir / "loop_claim").exists()


def test_a_receipt_appearing_during_claim_releases_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    loop_dir = tmp_path / "state" / "runs" / loop_id
    receipt = loop_dir / "loop_receipt.json"
    exists = Path.exists
    checks = 0

    def appears(path: Path) -> bool:
        nonlocal checks
        if path == receipt:
            checks += 1
            if checks == 2:
                assert (loop_dir / "loop_claim").is_file()
                receipt.write_text("{}\n")
        return exists(path)

    monkeypatch.setattr(Path, "exists", appears)
    with pytest.raises(
        LoopReceiptExistsError, match="already claimed and has a receipt"
    ):
        ports.claim_loop_receipt(loop_id)
    assert receipt.is_file()
    assert not (loop_dir / "loop_claim").exists()


def test_loop_receipt_is_published_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    loop_dir = tmp_path / "state" / "runs" / loop_id
    receipt = loop_dir / "loop_receipt.json"
    replace = os.replace
    replacements: list[Path] = []

    def publish(source: Path, destination: Path) -> None:
        assert source.parent == receipt.parent
        assert destination == receipt
        assert not receipt.exists()
        assert json.loads(source.read_text()) == {"status": "done"}
        replacements.append(source)
        replace(source, destination)

    monkeypatch.setattr(os, "replace", publish)
    ports.write_loop_receipt(loop_id, {"status": "done"})
    assert len(replacements) == 1
    assert json.loads(receipt.read_text()) == {"status": "done"}
    assert not (loop_dir / "loop_claim").exists()
    assert not replacements[0].exists()


def test_failed_receipt_write_still_releases_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    loop_dir = tmp_path / "state" / "runs" / loop_id

    def fail(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        ports.write_loop_receipt(loop_id, {"status": "done"})
    assert not (loop_dir / "loop_claim").exists()
    assert list(loop_dir.iterdir()) == []


def test_resume_claim_archives_each_prior_receipt(tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    ports.write_loop_receipt(loop_id, {"segment": 0})
    loop_dir = tmp_path / "state" / "runs" / loop_id
    for number in (1, 2):
        ports.claim_loop_receipt(loop_id, resume=True)
        assert (loop_dir / "loop_claim").exists()
        assert not (loop_dir / "loop_receipt.json").exists()
        assert json.loads((loop_dir / f"loop_receipt.{number}.json").read_text()) == {
            "segment": number - 1
        }
        ports.write_loop_receipt(loop_id, {"segment": number})
    assert ports.load_loop_receipt(loop_id) == {"segment": 2}


def test_resume_claim_without_receipt_releases_claim(tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    with pytest.raises(ResumeRefusedError, match="no receipt"):
        ports.claim_loop_receipt(loop_id, resume=True)
    assert not (tmp_path / "state" / "runs" / loop_id / "loop_claim").exists()


def test_held_claim_refuses_resume_without_archiving(tmp_path: Path) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    ports.claim_loop_receipt(loop_id)
    loop_dir = tmp_path / "state" / "runs" / loop_id
    (loop_dir / "loop_receipt.json").write_text("{}")
    with pytest.raises(LoopReceiptExistsError, match="already claimed"):
        ports.claim_loop_receipt(loop_id, resume=True)
    assert (loop_dir / "loop_claim").exists()
    assert (loop_dir / "loop_receipt.json").exists()
    assert list(loop_dir.glob("loop_receipt.*.json")) == []


@pytest.mark.parametrize("text", [None, "garbage", "[]", "null", '"text"'])
def test_load_loop_receipt_absent_or_invalid(tmp_path: Path, text: str | None) -> None:
    ports = _ports(tmp_path)
    loop_id = str(uuid.uuid4())
    if text is not None:
        loop_dir = tmp_path / "state" / "runs" / loop_id
        loop_dir.mkdir(parents=True)
        (loop_dir / "loop_receipt.json").write_text(text)
    assert ports.load_loop_receipt(loop_id) is None


def test_resume_end_to_end_archives_interruption_and_accepts(
    tree: Path, tmp_path: Path
) -> None:
    calls = 0

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        run_id = str(uuid.uuid4())
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        if calls in (2, 3):
            return subprocess.CompletedProcess(
                argv, 1, json.dumps({"run_id": run_id}), "failed"
            )
        reply = (
            {
                "actions": [
                    {
                        "tool": "edit",
                        "file_path": "src/m.py",
                        "old_string": "return 0",
                        "new_string": "return a + b",
                    }
                ]
            }
            if calls == 1
            else {"actions": [{"tool": "finish", "summary": "tests/test_m.py passes"}]}
        )
        (run_dir / "result.txt").write_text(json.dumps(reply))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    request = _request(tree)
    ports = _ports(tmp_path, runner)
    handler = HandlerDelegatedCodeEditOrchestrator(ports)
    result = handler.run(request)
    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert result.resumable
    prior = ports.load_loop_receipt(request.correlation_id)
    assert prior is not None
    assert json.loads(json.dumps(prior))["resume"]["last_good_turn"] == 1
    result = handler.run(request, resume=True)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.turns == 2
    loop_dir = tmp_path / "state" / "runs" / request.correlation_id
    receipt = json.loads((loop_dir / "loop_receipt.json").read_text())
    assert receipt["result"]["status"] == "accepted"
    assert receipt["resumes"] == 1
    assert json.loads((loop_dir / "loop_receipt.1.json").read_text()) == prior
    assert not (loop_dir / "loop_claim").exists()
    assert calls == 4
