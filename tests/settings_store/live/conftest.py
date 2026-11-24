# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live Valkey probes are collected only when the lab run names the store it probes (never skipped).

The probe module is named ``probe_*.py`` so the hosted shard planner, which deals every ``test_*.py`` file to a
shard, never hands it to a runner that holds no Valkey. This hook collects it when the lab run sets the variable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_collect_file(
    file_path: Path, parent: pytest.Collector
) -> pytest.Module | None:
    if os.environ.get("ONEX_VALKEY_ACL_PROBE") and file_path.name.startswith("probe_"):
        return pytest.Module.from_parent(parent, path=file_path)
    return None
