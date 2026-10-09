# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A socket path short enough for AF_UNIX on every lab host (macOS allows 104 bytes)."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="oe-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)
