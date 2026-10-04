# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise atomic-write cleanup and preserve unexpected filesystem errors."""

import errno
import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_output_materialize_effect.handlers import (
    handler_delegation_output_materialize as handler,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation", ["write", "fsync", "rename"])
def test_failed_atomic_write_removes_temporary_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    target = tmp_path / "result.txt"
    target.write_bytes(b"original")
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open
    opened: list[int] = []
    failure = OSError(errno.EIO, "injected I/O failure")

    def open_file(name: str, flags: int, mode: int, *, dir_fd: int) -> int:
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, operation, fail)
    try:
        with pytest.raises(OSError, match="injected I/O failure") as caught:
            handler._write_atomically("result.txt", b"replacement", root_fd)
        assert caught.value is failure
        assert target.read_bytes() == b"original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["result.txt"]
        assert len(opened) == 1
        with pytest.raises(OSError, match="Bad file descriptor") as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        os.close(root_fd)


def test_partial_writes_finish_all_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write
    sizes: list[int] = []

    def short_write(fd: int, data: bytes | memoryview) -> int:
        sizes.append(len(data))
        return original_write(fd, data[:2])

    monkeypatch.setattr(os, "write", short_write)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        handler._write_atomically("result.txt", b"abcdefg", root_fd)
    finally:
        os.close(root_fd)
    assert sizes == [7, 5, 3, 1]
    assert (tmp_path / "result.txt").read_bytes() == b"abcdefg"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["result.txt"]


def test_unexpected_child_open_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    failure = PermissionError(errno.EACCES, "injected directory permission error")

    def fail(*args: object, **kwargs: object) -> int:
        raise failure

    monkeypatch.setattr(os, "open", fail)
    try:
        with pytest.raises(PermissionError) as caught:
            handler._open_child_dir("child", root_fd)
        assert caught.value is failure
    finally:
        os.close(root_fd)
