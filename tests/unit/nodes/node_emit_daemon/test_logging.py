# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for RotatingFileHandler with gzip in node_emit_daemon (OMN-10124)."""

from __future__ import annotations

import gzip
import logging
import logging.handlers
from pathlib import Path

import pytest


@pytest.mark.unit
def test_daemon_logging_uses_rotating_file_handler(tmp_path: Path) -> None:
    from omnimarket.nodes.node_emit_daemon.__main__ import _configure_logging

    log_path = tmp_path / "test.log"
    daemon_logger = _configure_logging(str(log_path), max_bytes=1024, backup_count=3)

    handlers = [
        h
        for h in daemon_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1, "exactly one RotatingFileHandler expected"
    h = handlers[0]
    assert h.maxBytes == 1024
    assert h.backupCount == 3
    assert str(h.baseFilename) == str(log_path)


@pytest.mark.unit
def test_rotation_produces_gzipped_backup(tmp_path: Path) -> None:
    """After rotation, backup files must be valid gzip archives."""
    from omnimarket.nodes.node_emit_daemon.__main__ import _configure_logging

    log_path = tmp_path / "test.log"
    daemon_logger = _configure_logging(str(log_path), max_bytes=100, backup_count=2)

    # Write enough data to force at least one rotation
    for _i in range(50):
        daemon_logger.warning("x" * 50)

    # Force flush
    for h in daemon_logger.handlers:
        h.flush()

    backups = list(tmp_path.glob("test.log.*.gz"))
    assert len(backups) >= 1, (
        f"expected gzipped backups, got: {list(tmp_path.iterdir())}"
    )

    for backup in backups:
        # gunzip -t equivalent: open and read to verify integrity
        try:
            with gzip.open(backup, "rb") as f:
                f.read()
        except Exception as exc:
            pytest.fail(f"{backup} is not a valid gzip file: {exc}")


@pytest.mark.unit
def test_configure_logging_does_not_touch_root_logger(tmp_path: Path) -> None:
    """_configure_logging must use a daemon-specific logger, not root."""
    import logging

    from omnimarket.nodes.node_emit_daemon.__main__ import _configure_logging

    root_handler_count_before = len(logging.getLogger().handlers)
    log_path = tmp_path / "daemon.log"
    _configure_logging(str(log_path), max_bytes=1024, backup_count=1)
    root_handler_count_after = len(logging.getLogger().handlers)

    assert root_handler_count_before == root_handler_count_after, (
        "_configure_logging must not add handlers to the root logger"
    )


@pytest.mark.unit
def test_configure_logging_default_values(tmp_path: Path) -> None:
    """Default rotation thresholds: 100 MB, 5 backups."""
    from omnimarket.nodes.node_emit_daemon.__main__ import _configure_logging

    log_path = tmp_path / "default.log"
    daemon_logger = _configure_logging(str(log_path))

    handlers = [
        h
        for h in daemon_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1
    h = handlers[0]
    assert h.maxBytes == 100 * 1024 * 1024, "default max_bytes should be 100 MB"
    assert h.backupCount == 5, "default backup_count should be 5"
