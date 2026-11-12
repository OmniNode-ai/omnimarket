# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for FilesystemRollArtifactStore (OMN-24013)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.adapters.adapter_filesystem import (
    FilesystemRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols.protocol_roll_artifact_store import (
    RollArtifactStoreError,
)

pytestmark = pytest.mark.unit


class TestFilesystemRollArtifactStore:
    def test_write_lands_under_root(self, tmp_path: Path) -> None:
        store = FilesystemRollArtifactStore(
            root=tmp_path, retention=timedelta(days=1), clock=lambda: datetime.now(timezone.utc)
        )
        ref = store.write("job1/api.log", b"hello")
        assert (tmp_path / "job1" / "api.log").exists()
        assert ref == str(tmp_path / "job1" / "api.log")

    def test_path_traversal_refused(self, tmp_path: Path) -> None:
        store = FilesystemRollArtifactStore(
            root=tmp_path, retention=timedelta(days=1), clock=lambda: datetime.now(timezone.utc)
        )
        with pytest.raises(RollArtifactStoreError):
            store.write("../escape.log", b"bad")

    def test_files_older_than_retention_pruned(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=30)
        mock_clock = MagicMock(side_effect=[now, now])
        store = FilesystemRollArtifactStore(
            root=tmp_path, retention=timedelta(days=7), clock=mock_clock
        )
        old_file = tmp_path / "old.log"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"old")
        import os
        import time

        old_ts = old_time.timestamp()
        os.utime(old_file, (old_ts, old_ts))
        store.write("new.log", b"new")
        assert not old_file.exists()
