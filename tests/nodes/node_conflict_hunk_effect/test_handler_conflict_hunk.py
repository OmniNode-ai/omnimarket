# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerConflictHunk [OMN-8992].

TDD cases:
  1. Successful resolution — commits, emits success event
  2. Validation failure — LLM returns residual conflict markers → fail event, no commit
  3. LLM RuntimeError → re-raises (no swallow)
  4. routing_policy resolution — resolve_routing_policy called; None routing_policy → ValueError
  5. Blocked file rejection — file outside src/**,tests/** → fail event emitted
  6. No conflict markers → fail event, no commit
  7. is_noop=True when LLM returns identical file content
  8. Patch size guard — net delta > 50 lines → fail event
  9. pytest gate failure → fail event, no commit
  10. Python syntax validation failure → fail event
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_conflict_hunk_effect.handlers.handler_conflict_hunk import (
    HandlerConflictHunk,
    _extract_hunk_context,
    _find_conflict_files,
    _net_line_delta,
)
from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_resolved_event import (
    ModelConflictResolvedEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelConflictHunkCommand,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = "OmniNode-ai/omnimarket"
PR_NUM = 77
ROUTING_POLICY: dict[str, Any] = {
    "primary": "qwen3-coder-30b",
    "fallback": "glm-4.5",
    "fallback_allowed_roles": ["conflict_resolver"],
    "max_tokens": 8192,
}

_CONFLICT_FILE_CONTENT = textwrap.dedent("""\
    def foo():
    <<<<<<< HEAD
        return 1
    =======
        return 2
    >>>>>>> feature-branch
""")

_RESOLVED_CONTENT = textwrap.dedent("""\
    def foo():
        return 1
""")


def _make_command(
    head_ref: str = "feature/some-work",
    conflict_files: list[str] | None = None,
    routing_policy: dict[str, Any] | None = None,
) -> ModelConflictHunkCommand:
    return ModelConflictHunkCommand(
        pr_number=PR_NUM,
        repo=REPO,
        head_ref_name=head_ref,
        base_ref_name="main",
        conflict_files=conflict_files or ["src/foo.py"],
        correlation_id=uuid4(),
        run_id="OMN-8992-test-run",
        routing_policy=routing_policy or ROUTING_POLICY,
    )


def _make_subprocess_fn(
    wt_root: Path,
    conflict_content: str = _CONFLICT_FILE_CONTENT,
    pytest_rc: int = 0,
    commit_sha: str = "abc1234",
    git_add_rc: int = 0,
    git_commit_rc: int = 0,
    scope_diff_files: list[str] | None = None,
) -> Any:
    """Build a deterministic subprocess_run_fn that operates on a real temp dir."""
    import os

    def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        cmd_str = " ".join(cmd)

        # Worktree add: create the directory + write conflict file
        if "worktree" in cmd and "add" in cmd and "--force" not in cmd:
            wt_path = Path(cmd[-2])
            wt_path.mkdir(parents=True, exist_ok=True)
            # Create fake .git and src/
            (wt_path / ".git").mkdir(exist_ok=True)
            (wt_path / "src").mkdir(exist_ok=True)
            (wt_path / "tests").mkdir(exist_ok=True)
            (wt_path / "src" / "foo.py").write_text(conflict_content, encoding="utf-8")
            return 0, "", ""

        # Worktree remove: clean up
        if "worktree" in cmd and "remove" in cmd:
            import shutil

            target = Path(cmd[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""

        # git fetch
        if "fetch" in cmd:
            return 0, "", ""

        # git checkout -B <branch>
        if "checkout" in cmd and "-B" in cmd:
            return 0, "", ""

        # git checkout -- . (abort scope check)
        if "checkout" in cmd and "--" in cmd and "." in cmd:
            return 0, "", ""

        # git diff --name-only HEAD (scope check)
        if "diff" in cmd and "--name-only" in cmd and "HEAD" in cmd:
            files = scope_diff_files if scope_diff_files is not None else ["src/foo.py"]
            return 0, "\n".join(files) + "\n", ""

        # pytest gate
        if "pytest" in cmd_str:
            return (
                pytest_rc,
                "test output" if pytest_rc == 0 else "",
                "FAILED" if pytest_rc != 0 else "",
            )

        # git add
        if cmd[1:3] == ["-C", str(wt_root)] and "add" in cmd:
            return git_add_rc, "", "" if git_add_rc == 0 else "add failed"

        # find git add by checking the cmd contains "add" (and not worktree add)
        if "add" in cmd and "worktree" not in cmd:
            return git_add_rc, "", "" if git_add_rc == 0 else "add failed"

        # git commit
        if "commit" in cmd:
            return git_commit_rc, "", "" if git_commit_rc == 0 else "commit failed"

        # git rev-parse HEAD
        if "rev-parse" in cmd:
            return 0, commit_sha, ""

        # source clone .git check
        if (
            os.path.exists(os.path.join(cmd[2], ".git"))
            if len(cmd) > 2 and "-C" in cmd
            else False
        ):
            return 0, "", ""

        return 0, "", ""

    return _run


# ---------------------------------------------------------------------------
# TDD case 1: Successful resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_resolution(tmp_path: Path) -> None:
    """Happy path: conflict file resolved, committed, success event emitted."""
    # Create a fake source clone
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    wt_root_base = tmp_path / "worktrees"
    wt_root_base.mkdir()

    resolved_calls: list[str] = []

    def llm_call(
        file_path: str, hunk_context: str, routing_policy: dict[str, Any]
    ) -> tuple[str, bool]:
        resolved_calls.append(file_path)
        return _RESOLVED_CONTENT, False

    cmd = _make_command()
    wt_path_holder: list[Path] = []

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        nonlocal wt_path_holder

        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt_path = Path(cmd_list[-2])
            wt_path.mkdir(parents=True, exist_ok=True)
            (wt_path / ".git").mkdir(exist_ok=True)
            (wt_path / "src").mkdir(exist_ok=True)
            (wt_path / "tests").mkdir(exist_ok=True)
            (wt_path / "src" / "foo.py").write_text(
                _CONFLICT_FILE_CONTENT, encoding="utf-8"
            )
            wt_path_holder.append(wt_path)
            return 0, "", ""

        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""

        if "fetch" in cmd_list:
            return 0, "", ""

        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""

        if "checkout" in cmd_list and "--" in cmd_list:
            return 0, "", ""

        if "diff" in cmd_list and "--name-only" in cmd_list:
            return 0, "src/foo.py\n", ""

        if "rev-parse" in cmd_list:
            return 0, "deadbeef1234", ""

        if "uv" in cmd_list and "pytest" in cmd_list:
            return 0, "1 passed", ""

        if "add" in cmd_list and "worktree" not in cmd_list:
            return 0, "", ""

        if "commit" in cmd_list:
            return 0, "", ""

        return 0, "", ""

    import os

    original_env = os.environ.get("ONEX_CONFLICT_SOURCE_CLONE_ROOT")
    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(wt_root_base)

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call,
            subprocess_run_fn=subprocess_fn,
        )
        output = await handler.handle(cmd)
    finally:
        if original_env is None:
            os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        else:
            os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = original_env
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    assert output is not None
    assert len(output.events) == 1
    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is True
    assert event.resolution_committed is True
    assert event.is_noop is False
    assert event.commit_sha == "deadbeef1234"
    assert "src/foo.py" in event.resolved_files
    assert event.used_fallback is False
    assert "src/foo.py" in resolved_calls


# ---------------------------------------------------------------------------
# TDD case 2: Validation failure — residual conflict markers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_residual_markers(tmp_path: Path) -> None:
    """LLM returns text still containing conflict markers → fail event."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        # Returns text still containing conflict markers
        return "<<<<<<< HEAD\nstill broken\n", False

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert event.resolution_committed is False
    assert "conflict markers" in (event.error or "")


# ---------------------------------------------------------------------------
# TDD case 3: LLM RuntimeError re-raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_error_reraises(tmp_path: Path) -> None:
    """LLM raises RuntimeError → handler re-raises without swallowing."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        raise RuntimeError("LLM endpoint unreachable")

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        with pytest.raises(RuntimeError, match="LLM endpoint unreachable"):
            await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)


# ---------------------------------------------------------------------------
# TDD case 4: routing_policy resolution — None routing_policy → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_routing_policy_raises(tmp_path: Path) -> None:
    """Envelope with routing_policy=None → resolve_routing_policy raises ValueError."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    cmd = ModelConflictHunkCommand(
        pr_number=PR_NUM,
        repo=REPO,
        head_ref_name="feature/work",
        base_ref_name="main",
        conflict_files=["src/foo.py"],
        correlation_id=uuid4(),
        run_id="OMN-8992-test",
        routing_policy={},  # empty dict will fail schema validation
    )

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(subprocess_run_fn=subprocess_fn)
        with pytest.raises(ValueError, match="routing_policy"):
            await handler.handle(cmd)
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)


# ---------------------------------------------------------------------------
# TDD case 5: Blocked file rejection — outside allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_outside_allowlist_emits_failure(tmp_path: Path) -> None:
    """File outside src/**,tests/** → fail event emitted (not exception)."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            # File in disallowed location
            (wt / "scripts").mkdir(exist_ok=True)
            (wt / "scripts" / "build.sh").write_text(
                _CONFLICT_FILE_CONTENT, encoding="utf-8"
            )
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(subprocess_run_fn=subprocess_fn)
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "allowlist" in (event.error or "")


# ---------------------------------------------------------------------------
# TDD case 6: No conflict markers → fail event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_conflict_markers_fails(tmp_path: Path) -> None:
    """No conflict markers in worktree → fail event (no exception)."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            # No conflict markers
            (wt / "src" / "clean.py").write_text(
                "def foo(): return 1\n", encoding="utf-8"
            )
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(subprocess_run_fn=subprocess_fn)
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "No conflict markers" in (event.error or "")


# ---------------------------------------------------------------------------
# TDD case 7: is_noop=True when LLM returns identical content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_when_llm_returns_same_content(tmp_path: Path) -> None:
    """LLM returns same content as current file → is_noop=True, no commit."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        # Return the identical conflict content — noop
        return _CONFLICT_FILE_CONTENT, False

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.is_noop is True
    assert event.resolution_committed is False
    assert event.success is True


# ---------------------------------------------------------------------------
# TDD case 8: Patch size guard — net delta > 50 lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_size_guard(tmp_path: Path) -> None:
    """LLM resolution changes > 50 net lines → fail event."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    # Generate resolved content with 100 extra lines
    big_resolved = _RESOLVED_CONTENT + "\n".join(f"# line {i}" for i in range(100))

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        return big_resolved, False

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "net changed lines" in (event.error or "")


# ---------------------------------------------------------------------------
# TDD case 9: pytest gate failure → fail event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pytest_gate_failure(tmp_path: Path) -> None:
    """pytest exits non-zero in worktree → fail event, no commit."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        return _RESOLVED_CONTENT, False

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        cmd_str = " ".join(str(c) for c in cmd_list)
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "--" in cmd_list:
            return 0, "", ""
        if "diff" in cmd_list and "--name-only" in cmd_list:
            return 0, "src/foo.py\n", ""
        if "pytest" in cmd_str:
            return 1, "", "FAILED: test_foo AssertionError"
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "pytest gate failed" in (event.error or "")
    assert event.resolution_committed is False


# ---------------------------------------------------------------------------
# TDD case 10: Python syntax validation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_syntax_validation_failure(tmp_path: Path) -> None:
    """LLM returns invalid Python → fail event with syntax error message."""
    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    def llm_call(fp: str, ctx: str, rp: dict[str, Any]) -> tuple[str, bool]:
        return "def foo(\n    broken syntax!!!\n", False

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        if "worktree" in cmd_list and "add" in cmd_list and "--force" not in cmd_list:
            wt = Path(cmd_list[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").mkdir(exist_ok=True)
            (wt / "src").mkdir(exist_ok=True)
            (wt / "src" / "foo.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
            return 0, "", ""
        if "worktree" in cmd_list and "remove" in cmd_list:
            import shutil

            target = Path(cmd_list[-1])
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, "", ""
        if "fetch" in cmd_list:
            return 0, "", ""
        if "checkout" in cmd_list and "-B" in cmd_list:
            return 0, "", ""
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(
            llm_call_fn=llm_call, subprocess_run_fn=subprocess_fn
        )
        output = await handler.handle(_make_command())
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "invalid Python syntax" in (event.error or "")


# ---------------------------------------------------------------------------
# Branch guard: refuses protected heads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_guard_protected_head(tmp_path: Path) -> None:
    """head_ref=main → fail event before worktree creation."""
    import os

    os.environ["ONEX_CONFLICT_SOURCE_CLONE_ROOT"] = str(tmp_path)
    os.environ["ONEX_CONFLICT_WORKTREE_ROOT"] = str(tmp_path / "wt")

    repo_key = REPO.replace("/", "__")
    source_clone = tmp_path / repo_key
    source_clone.mkdir()
    (source_clone / ".git").mkdir()

    def subprocess_fn(
        cmd_list: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        return 0, "", ""

    try:
        handler = HandlerConflictHunk(subprocess_run_fn=subprocess_fn)
        output = await handler.handle(_make_command(head_ref="main"))
    finally:
        os.environ.pop("ONEX_CONFLICT_SOURCE_CLONE_ROOT", None)
        os.environ.pop("ONEX_CONFLICT_WORKTREE_ROOT", None)

    event = output.events[0]
    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is False
    assert "protected_head_ref" in (event.error or "")


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


def test_find_conflict_files_detects_markers(tmp_path: Path) -> None:
    """_find_conflict_files returns files containing <<<<<<< markers."""
    (tmp_path / "conflict.py").write_text(_CONFLICT_FILE_CONTENT, encoding="utf-8")
    (tmp_path / "clean.py").write_text("def foo(): return 1\n", encoding="utf-8")

    result = _find_conflict_files(tmp_path)
    assert len(result) == 1
    assert result[0].name == "conflict.py"


def test_net_line_delta_counts_change() -> None:
    """_net_line_delta returns absolute difference in line count."""
    orig = "a\nb\nc\n"
    resolved = "a\nb\nc\nd\ne\n"
    assert _net_line_delta(orig, resolved) == 2


def test_extract_hunk_context_includes_surroundings() -> None:
    """_extract_hunk_context includes lines before and after conflict blocks."""
    text = "line1\nline2\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nline8\n"
    ctx = _extract_hunk_context(text, context_lines=2)
    assert "line1" in ctx
    assert "<<<<<<< HEAD" in ctx
    assert ">>>>>>> branch" in ctx
    assert "line8" in ctx
