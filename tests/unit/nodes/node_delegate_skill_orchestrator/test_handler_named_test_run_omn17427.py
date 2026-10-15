# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local execution evidence for the named-test runner (OMN-17427)."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from textwrap import dedent

import pytest

from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestFile,
    ModelNamedTestRunRequest,
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_named_test_run import (
    HandlerNamedTestRun,
)

TEST_ID = "test_add.py::test_add"
TEST_SOURCE = "from add_impl import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
CORRECT_ANSWER = "```python\ndef add(a, b):\n    return a + b\n```"


def _inline_request(
    answer: str = CORRECT_ANSWER,
    *,
    source: str = TEST_SOURCE,
    test_ids: tuple[str, ...] = (TEST_ID,),
    timeout_seconds: int = 30,
) -> ModelNamedTestRunRequest:
    return ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            test_files=(ModelNamedTestFile(path="test_add.py", content=source),),
            answer_path="add_impl.py",
            test_ids=test_ids,
        ),
        answer_content=answer,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.unit
def test_fenced_correct_answer_passes() -> None:
    result = HandlerNamedTestRun().handle(_inline_request())

    assert result.status is EnumNamedTestRunStatus.COMPLETED
    assert result.run is not None
    assert result.run.test_ids == (TEST_ID,)
    assert result.run.failed_test_ids == ()
    assert result.run.exit_code == 0
    assert result.detail == "all 1 named test(s) passed"


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        "def add(a, b):\n    return a - b",
        "def add(a, b):\n    pass",
        "def add(: invalid syntax",
    ],
    ids=["wrong", "stub", "syntax-error"],
)
def test_bad_answer_fails(answer: str) -> None:
    result = HandlerNamedTestRun().handle(_inline_request(answer))

    assert result.status is EnumNamedTestRunStatus.COMPLETED
    assert result.run is not None
    assert result.run.failed_test_ids == (TEST_ID,)
    assert result.run.exit_code != 0
    assert result.detail.startswith(f"1 of 1 named test(s) failed: {TEST_ID}")
    assert "Error" in result.detail


@pytest.mark.unit
def test_workspace_copy_leaves_original_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_add.py").write_text(TEST_SOURCE, encoding="utf-8")
    request = ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            workspace_root=str(workspace),
            answer_path="add_impl.py",
            test_ids=(TEST_ID,),
        ),
        answer_content=CORRECT_ANSWER,
    )

    result = HandlerNamedTestRun().handle(request)

    assert result.run is not None
    assert result.run.passed, result.detail
    assert not (workspace / "add_impl.py").exists()
    assert sorted(path.name for path in workspace.iterdir()) == ["test_add.py"]
    assert (workspace / "test_add.py").read_text(encoding="utf-8") == TEST_SOURCE


@pytest.mark.unit
def test_timeout_kills_process_group() -> None:
    source = dedent("""\
        import subprocess
        import sys
        import time

        def test_add():
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            time.sleep(30)
    """)
    started = time.monotonic()

    result = HandlerNamedTestRun().handle(
        _inline_request(source=source, timeout_seconds=2)
    )

    assert time.monotonic() - started < 10
    assert result.status is EnumNamedTestRunStatus.COMPLETED
    assert result.run is not None
    assert result.run.failed_test_ids == (TEST_ID,)
    assert result.run.exit_code == 124
    assert "timed out after 2s" in result.detail


@pytest.mark.unit
@pytest.mark.parametrize("is_file", [False, True], ids=["missing", "file"])
def test_unusable_workspace_is_refused(tmp_path: Path, is_file: bool) -> None:
    workspace = tmp_path / "workspace"
    if is_file:
        workspace.touch()
    request = ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            workspace_root=str(workspace),
            answer_path="add_impl.py",
            test_ids=(TEST_ID,),
        ),
        answer_content=CORRECT_ANSWER,
    )

    result = HandlerNamedTestRun().handle(request)

    assert result.status is EnumNamedTestRunStatus.REFUSED
    assert result.run is None
    assert str(workspace) in result.detail
    assert ("is not a directory" if is_file else "does not exist") in result.detail


@pytest.mark.unit
def test_parent_secret_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMN_SECRET_PROBE", "must-not-reach-the-test")
    source = dedent("""\
        import os
        from pathlib import Path
        from add_impl import add

        def test_add():
            assert os.environ.get("OMN_SECRET_PROBE") is None
            assert os.environ["HOME"] == str(Path.cwd())
            assert os.environ["PYTHONPATH"] == str(Path.cwd())
            assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
            assert os.environ["LANG"] == "C.UTF-8"
            assert add(2, 3) == 5
    """)

    result = HandlerNamedTestRun().handle(_inline_request(source=source))

    assert result.run is not None
    assert result.run.passed, result.detail


@pytest.mark.unit
def test_named_tests_run_separately_and_keep_failure_order() -> None:
    source = dedent("""\
        from pathlib import Path

        def test_first():
            Path("first-ran").touch()
            assert False, "first failure evidence"

        def test_pass():
            assert Path("first-ran").exists()
            Path("middle-ran").touch()

        def test_last():
            assert Path("middle-ran").exists()
            assert False, "last failure evidence"
    """)
    test_ids = (
        "test_add.py::test_first",
        "test_add.py::test_pass",
        "test_add.py::test_last",
    )

    result = HandlerNamedTestRun().handle(
        _inline_request(source=source, test_ids=test_ids)
    )

    assert result.run is not None
    assert result.run.test_ids == test_ids
    assert result.run.failed_test_ids == (test_ids[0], test_ids[2])
    assert result.run.exit_code == 1
    assert "2 of 3 named test(s) failed" in result.detail
    assert "first failure evidence" in result.detail
    assert "last failure evidence" not in result.detail


@pytest.mark.unit
def test_no_tests_collected_is_failure() -> None:
    result = HandlerNamedTestRun().handle(
        _inline_request(source="", test_ids=("test_add.py",))
    )

    assert result.run is not None
    assert result.run.failed_test_ids == ("test_add.py",)
    assert result.run.exit_code == 5


@pytest.mark.unit
@pytest.mark.parametrize("relative", ["add_impl.py", "test_add.py", "nested"])
def test_copied_symlink_cannot_redirect_writes(tmp_path: Path, relative: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside if relative == "nested" else outside / relative
    (workspace / relative).symlink_to(target, target_is_directory=relative == "nested")
    answer_path = "nested/add_impl.py" if relative == "nested" else "add_impl.py"
    request = ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            workspace_root=str(workspace),
            test_files=(ModelNamedTestFile(path="test_add.py", content=TEST_SOURCE),),
            answer_path=answer_path,
            test_ids=(TEST_ID,),
        ),
        answer_content=CORRECT_ANSWER,
    )

    result = HandlerNamedTestRun().handle(request)

    assert result.status is EnumNamedTestRunStatus.REFUSED
    assert result.run is None
    assert "outside the sandbox" in result.detail
    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_preparation_oserror_is_refused() -> None:
    request = ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            test_files=(ModelNamedTestFile(path="parent", content="a file"),),
            answer_path="parent/add_impl.py",
            test_ids=(TEST_ID,),
        ),
        answer_content=CORRECT_ANSWER,
    )

    result = HandlerNamedTestRun().handle(request)

    assert result.status is EnumNamedTestRunStatus.REFUSED
    assert result.run is None
    assert "FileExistsError" in result.detail


@pytest.mark.unit
def test_failure_output_is_bounded_and_sandbox_is_removed() -> None:
    source = dedent("""\
        from pathlib import Path

        def test_add():
            print("x" * 6000)
            print("sandbox=" + str(Path.cwd()), flush=True)
            raise AssertionError("failure-tail")
    """)

    result = HandlerNamedTestRun().handle(_inline_request(source=source))

    assert result.run is not None
    assert result.run.failed_test_ids == (TEST_ID,)
    assert len(result.detail) <= 4000
    assert "failure-tail" in result.detail
    sandbox_line = next(
        line for line in result.detail.splitlines() if line.startswith("sandbox=")
    )
    assert not Path(sandbox_line.removeprefix("sandbox=")).exists()


@pytest.mark.unit
def test_posix_resource_limits() -> None:
    if os.name != "posix":
        pytest.skip("resource limits are POSIX-only")
    source = dedent("""\
        import resource

        def test_add():
            assert resource.getrlimit(resource.RLIMIT_CPU) == (35, 35)
            assert resource.getrlimit(resource.RLIMIT_AS) == (2 * 1024**3,) * 2
            assert resource.getrlimit(resource.RLIMIT_FSIZE) == (64 * 1024**2,) * 2
    """)

    result = HandlerNamedTestRun().handle(_inline_request(source=source))

    assert result.run is not None
    assert result.run.passed, result.detail


@pytest.mark.unit
@pytest.mark.parametrize("swapped", ["directory", "file"])
def test_symlink_swapped_in_after_the_check_cannot_redirect_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str
) -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
        handler_named_test_run,
    )

    sandbox = (tmp_path / "sandbox").resolve()
    (sandbox / "nested").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_resolve = Path.resolve

    def resolve_then_swap(self: Path, strict: bool = False) -> Path:
        resolved = real_resolve(self, strict=strict)
        # The containment check sees a plain path; a symlink replaces it
        # before the write, as a concurrent writer could.
        if swapped == "directory":
            (sandbox / "nested").rmdir()
            (sandbox / "nested").symlink_to(outside, target_is_directory=True)
        else:
            (sandbox / "nested" / "add_impl.py").symlink_to(outside / "add_impl.py")
        monkeypatch.setattr(Path, "resolve", real_resolve)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_swap)

    with pytest.raises(OSError, match=r"symbolic links|not a directory") as raised:
        handler_named_test_run._write_file(sandbox, "nested/add_impl.py", "x = 1\n")
    assert raised.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_symlink_inside_the_sandbox_still_carries_a_write(tmp_path: Path) -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
        handler_named_test_run,
    )

    sandbox = (tmp_path / "sandbox").resolve()
    (sandbox / "real").mkdir(parents=True)
    (sandbox / "alias").symlink_to(sandbox / "real", target_is_directory=True)

    handler_named_test_run._write_file(sandbox, "alias/deep/add_impl.py", "x = 1\n")

    assert (sandbox / "real" / "deep" / "add_impl.py").read_text() == "x = 1\n"
