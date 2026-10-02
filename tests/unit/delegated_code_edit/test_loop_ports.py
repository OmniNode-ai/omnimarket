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
