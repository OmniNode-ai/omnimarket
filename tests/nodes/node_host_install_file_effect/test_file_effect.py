# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and failing-control chains of the host install file effect (OMN-23957).

* golden: snapshot copies the installed files to the backup and names the digest; read returns the text and digest;
  restore puts the backup back and the digest equals the snapshot digest; restore of an install that created the files
  (none existed before) removes them;
* error: restore with no backup manifest yields a failure result; a relative path is refused by the model; a text larger
  than the overlay's max_text_bytes yields a failure result;
* control: a handler variant whose restore skips the removal of files that did not exist is detected by the first test
  (assert the control leaves a file behind).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_install_file_effect.handlers.handler_host_install_file import (
    HandlerHostInstallFile,
    LocalHostInstallFiles,
)
from omnimarket.nodes.node_host_install_file_effect.models import (
    ModelHostInstallFileRequest,
    ModelHostInstallFileResult,
)
from omnimarket.nodes.node_host_install_file_effect.overlay import (
    ModelHostInstallFileOverlay,
)
from tests.nodes.node_host_install_file_effect.support import (
    RUN_ID,
    FakeHostInstallFiles,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

SERVICE = "/tmp/omni.service"
TIMER = "/tmp/omni.timer"
SERVICE_TEXT = "[Unit]\nDescription=omni\n"
TIMER_TEXT = "[Timer]\nOnBootSec=60\n"


def _overlay() -> ModelHostInstallFileOverlay:
    return overlay()


def _handler(files: FakeHostInstallFiles) -> HandlerHostInstallFile:
    return HandlerHostInstallFile(overlay=_overlay(), files=files)


def test_golden_chain_snapshot_read_restore() -> None:
    """Snapshot copies the installed pair, read returns the text and digest, restore puts the backup back."""
    files = FakeHostInstallFiles()
    files.seed(SERVICE, SERVICE_TEXT)
    files.seed(TIMER, TIMER_TEXT)
    handler = _handler(files)
    ov = _overlay()
    backup_dir = f"{ov.backup_root}/{RUN_ID}"

    snap = handler.handle(request(mode="snapshot", paths=(SERVICE, TIMER)))
    assert snap.status == "success"
    assert snap.present is True
    assert snap.mode == "snapshot"
    assert snap.backup_dir == backup_dir
    assert snap.digest != ""
    assert snap.text == SERVICE_TEXT + "\n" + TIMER_TEXT

    read = handler.handle(request(mode="read", paths=(SERVICE, TIMER)))
    assert read.status == "success"
    assert read.present is True
    assert read.digest == snap.digest
    assert read.text == snap.text

    files._files[SERVICE] = "[Unit]\nDescription=changed\n"
    files._files[TIMER] = "[Timer]\nOnBootSec=120\n"

    restored = handler.handle(request(mode="restore", paths=(SERVICE, TIMER)))
    assert restored.status == "success"
    assert restored.present is True
    assert restored.digest == snap.digest
    assert files.read_text(SERVICE) == SERVICE_TEXT
    assert files.read_text(TIMER) == TIMER_TEXT


def test_restore_of_created_files_removes_them() -> None:
    """Snapshot when no file existed records that; restore removes the files the install created."""
    files = FakeHostInstallFiles()
    handler = _handler(files)
    snap = handler.handle(request(mode="snapshot", paths=(SERVICE, TIMER)))
    assert snap.status == "success"
    assert snap.present is False
    assert snap.digest == ""
    assert snap.text == ""

    files.seed(SERVICE, "new service")
    files.seed(TIMER, "new timer")

    restored = handler.handle(request(mode="restore", paths=(SERVICE, TIMER)))
    assert restored.status == "success"
    assert restored.present is False
    assert restored.digest == ""
    assert not files.exists(SERVICE)
    assert not files.exists(TIMER)


def test_restore_with_no_backup_manifest_fails() -> None:
    files = FakeHostInstallFiles()
    handler = _handler(files)
    result = handler.handle(request(mode="restore", paths=(SERVICE, TIMER)))
    assert result.status == "failure"
    assert "manifest" in result.detail.lower()


def test_relative_path_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="not absolute"):
        request(paths=("relative/path.service",))


def test_text_over_max_text_bytes_fails() -> None:
    files = FakeHostInstallFiles()
    handler = _handler(files)
    ov = _overlay()
    big = "x" * (ov.max_text_bytes + 1)
    files.seed(SERVICE, big)
    result = handler.handle(request(mode="read", paths=(SERVICE, TIMER)))
    assert result.status == "failure"
    assert "max_text_bytes" in result.detail or "text" in result.detail.lower()


def test_failing_control_restore_skips_removal() -> None:
    """The control skips removal of files that did not exist: it leaves the created files behind, the real handler does not."""

    class SkipsRemoval(HandlerHostInstallFile):
        def _restore(
            self, request: ModelHostInstallFileRequest, backup_dir: str
        ) -> ModelHostInstallFileResult:
            return self._read(request, backup_dir)

    def created_after_snapshot(
        handler_class: type[HandlerHostInstallFile],
    ) -> FakeHostInstallFiles:
        files = FakeHostInstallFiles()
        handler = handler_class(overlay=_overlay(), files=files)
        snap = handler.handle(request(mode="snapshot", paths=(SERVICE, TIMER)))
        assert snap.present is False
        files.seed(SERVICE, "created service")
        files.seed(TIMER, "created timer")
        handler.handle(request(mode="restore", paths=(SERVICE, TIMER)))
        return files

    assert not created_after_snapshot(HandlerHostInstallFile).exists(SERVICE)
    assert created_after_snapshot(SkipsRemoval).exists(SERVICE)


def test_the_local_files_do_the_same_chain_on_a_real_directory(tmp_path: Path) -> None:
    """The real file I/O, not the fake: snapshot, a changed install, restore, and a created file removed."""
    service, timer = tmp_path / "unit.service", tmp_path / "unit.timer"
    service.write_text(SERVICE_TEXT)
    ov = _overlay().model_copy(update={"backup_root": str(tmp_path / "backups")})
    handler = HandlerHostInstallFile(overlay=ov, files=LocalHostInstallFiles())
    paths = (str(service), str(timer))
    snap = handler.handle(request(mode="snapshot", paths=paths))
    assert (snap.status, snap.present) == ("success", False)
    service.write_text("changed\n")
    timer.write_text("created\n")
    restored = handler.handle(request(mode="restore", paths=paths))
    assert restored.status == "success"
    assert service.read_text() == SERVICE_TEXT
    assert not timer.exists()
    assert restored.digest == snap.digest
