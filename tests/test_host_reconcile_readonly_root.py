# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check run on a read-only registry root returns the host's verdict, not exit 3.

Lanes run in a sandbox whose registry root is read-only, so creating
`.onex-reconcile-host.lock` there fails with Errno 30. Check mode mutates
nothing, so a root it cannot write to is not a reason to stop; repair mode
keeps the lock it always took and still stops.
"""

import errno
import os
import socket
from pathlib import Path
from typing import Literal

import pytest

from omnimarket.nodes.node_host_reconcile_effect.handlers import (
    handler_host_reconcile_effect as handler_module,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit

LOCK_NAME = ".onex-reconcile-host.lock"


@pytest.fixture
def read_only_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make creating the lock directory fail the way the lane sandbox does."""
    real_mkdir = Path.mkdir

    def mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == LOCK_NAME:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", mkdir)


@pytest.mark.parametrize(
    ("behind", "exit_code"),
    [(False, 0), (True, 2)],
    ids=["in-sync", "behind"],
)
def test_check_on_read_only_root_returns_the_host_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only_root: None,
    behind: bool,
    exit_code: int,
) -> None:
    root = setup_workspace(tmp_path, monkeypatch, behind=behind)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check")
    )
    assert result.exit_code == exit_code
    assert not any("Errno 30" in line for line in result.diagnostics)
    assert not any("INDETERMINATE" in line for line in result.diagnostics)
    assert not (root / LOCK_NAME).exists()


def test_repair_on_read_only_root_still_takes_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only_root: None
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "repair")
    )
    assert result.exit_code == 3
    assert any("Errno 30" in line for line in result.diagnostics)


def test_check_reports_a_live_repair_holder_and_leaves_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    lock = root / LOCK_NAME
    lock.mkdir()
    holder = (
        f"pid={os.getpid()}\nhost={socket.gethostname()}\n"
        "started_at=2026-10-09T00:00:00Z\nholder=host-reconcile\n"
    )
    (lock / "holder").write_text(holder)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, "check", max_holder_age_s=10**9)
    )
    assert result.exit_code == 4
    assert (lock / "holder").read_text() == holder


@pytest.mark.parametrize(
    ("mode", "exit_code"), [("check", 0), ("repair", 3)], ids=["check", "repair"]
)
def test_stale_lock_on_read_only_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["check", "repair"],
    exit_code: int,
) -> None:
    root = setup_workspace(tmp_path, monkeypatch)
    lock = root / LOCK_NAME
    lock.mkdir()
    (lock / "holder").write_text(f"pid=999999999\nhost={socket.gethostname()}\n")

    def rmtree(path: object, *args: object, **kwargs: object) -> None:
        raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(path))

    monkeypatch.setattr(handler_module.shutil, "rmtree", rmtree)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(
        command(root, mode)
    )
    assert result.exit_code == exit_code
    assert lock.exists()
