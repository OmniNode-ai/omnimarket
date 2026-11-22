# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test-file listings: the checkout's own tree (real backend) and a fixed list (tests')."""

from __future__ import annotations

from pathlib import Path

_TEST_ROOT = "tests"


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


class SourceTestFilesDisk:
    """Lists the files named ``test_*.py`` or ``*_test.py`` under the checkout's ``tests/`` tree."""

    def list_test_files(self, repo_root: str) -> tuple[str, ...] | None:
        root = Path(repo_root)
        tests = root / _TEST_ROOT
        if not tests.is_dir():
            # A checkout with no tests tree has no test file to cost; an unreadable durations
            # file has already escalated before this is asked.
            return ()
        try:
            return tuple(
                sorted(
                    path.relative_to(root).as_posix()
                    for path in tests.rglob("*.py")
                    if _is_test_file(path.name)
                )
            )
        except OSError:
            return None

    def file_exists(self, repo_root: str, path: str) -> bool:
        return (Path(repo_root) / path).is_file()


class SourceTestFilesInMemory:
    """Answers the listing it was built with; ``None`` stands for an unreadable tree."""

    def __init__(self, files: tuple[str, ...] | None = ()) -> None:
        self._files = None if files is None else tuple(sorted(files))

    def list_test_files(self, repo_root: str) -> tuple[str, ...] | None:
        return self._files

    def file_exists(self, repo_root: str, path: str) -> bool:
        return self._files is not None and path in self._files
