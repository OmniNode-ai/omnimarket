# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter tests of the installed units files reader.

The default adapter over a tmp directory holding a.service, b.service and c.timer returns a and
b only, with the commands joined by one space; a missing directory is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_job_claim_compute.adapters.installed_units_files import (
    InstalledUnitsFiles,
)

pytestmark = pytest.mark.unit


def test_installed_units_files_reads_service_files(tmp_path: Path) -> None:
    (tmp_path / "a.service").write_text("ExecStart=/bin/a --x\n", encoding="utf-8")
    (tmp_path / "b.service").write_text(
        "ExecStart=/bin/b --y\nExecStart=/bin/b --z\n", encoding="utf-8"
    )
    (tmp_path / "c.timer").write_text("ExecStart=/bin/c\n", encoding="utf-8")
    result = InstalledUnitsFiles().installed((str(tmp_path),), ".service")
    names = [unit.name for unit in result]
    assert names == ["a", "b"]
    assert result[0].command == "/bin/a --x"
    assert result[1].command == "/bin/b --y /bin/b --z"


def test_installed_units_files_skips_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = InstalledUnitsFiles().installed((str(missing),), ".service")
    assert result == ()
