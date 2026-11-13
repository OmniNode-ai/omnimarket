# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Collect live dev tag lineage tests only when ONEX_DEV_TAG_LINEAGE_LIVE=1.

A subdirectory conftest cannot register CLI options or markers, so the gate is
an environment variable; collected-then-skipped tests are refused by the
repo's skip-count ratchet.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    if not collection_path.name.startswith("test_"):
        return False
    return os.environ.get("ONEX_DEV_TAG_LINEAGE_LIVE") != "1"
