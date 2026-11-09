# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shard count of a narrowed test selection, from estimated test time.

Standard library only: scripts/ci/detect_test_paths.py loads this file by path
inside a CI environment that has no omnimarket install.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

FULL_SUITE_SPLIT_COUNT = 20
# The full suite's own per-shard share: about 7160 s over 20 shards.
TARGET_SHARD_SECONDS = 360


def path_count_split(selected_paths: list[str]) -> int:
    n = len(selected_paths)
    if n <= 2:
        return 1
    if n <= 5:
        return 2
    if n <= 10:
        return 3
    if n <= 16:
        return 4
    return 5


def estimated_seconds(selected_paths: list[str], durations_path: Path) -> float:
    """Sum the committed per-file durations of every test file a selection covers."""
    try:
        durations = json.loads(durations_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(durations, dict):
        return 0.0
    total = 0.0
    for test_file, seconds in durations.items():
        if not isinstance(seconds, (int, float)):
            continue
        if any(
            test_file == path or (path.endswith("/") and test_file.startswith(path))
            for path in selected_paths
        ):
            total += float(seconds)
    return total


def duration_split_count(
    selected_paths: list[str],
    durations_path: Path,
    target_shard_seconds: int = TARGET_SHARD_SECONDS,
) -> int:
    """Shard count that keeps each shard near the target share of test time."""
    estimated = estimated_seconds(selected_paths, durations_path)
    if estimated <= 0:
        return 1
    return min(
        FULL_SUITE_SPLIT_COUNT, max(1, math.ceil(estimated / target_shard_seconds))
    )


def split_count(
    selected_paths: list[str],
    durations_path: Path,
    target_shard_seconds: int = TARGET_SHARD_SECONDS,
) -> int:
    """Never fewer shards than the path-count rule, never more than the full matrix."""
    return max(
        path_count_split(selected_paths),
        duration_split_count(selected_paths, durations_path, target_shard_seconds),
    )
