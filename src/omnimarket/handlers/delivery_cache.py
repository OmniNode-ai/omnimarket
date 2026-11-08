# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-process caches for the files a projection delivery used to re-parse.

A runtime-dispatched projection writer builds one runner and opens one set of
topology-bound pools for every delivery, because no pool may outlive the event
loop that opened it. That left two pure-Python YAML parses on the hot path of
every delivery: the writer's own ``contract.yaml`` and the deployment topology
profile. On the dev runtime they held the GIL for about 36 percent of its busy
time (py-spy, 2026-11-06), with the topology profile alone at about 19 percent.

Both files are checked in with the image and do not change while a process
runs, so each is parsed once per process. A contract file is keyed by its path,
modification time and size, so a test that rewrites a temporary contract is
still read fresh. The cached contract is handed out as a deep copy, because
runners keep and sometimes edit their copy.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=256)
def _parse_contract(path: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size  # cache key only: a changed file is a different entry
    with open(path, encoding="utf-8") as handle:
        parsed: dict[str, Any] = yaml.safe_load(handle)
    return parsed


def load_contract(path: Path) -> dict[str, Any]:
    """Return the parsed contract at ``path``, parsing it once per file state."""
    stat = path.stat()
    return copy.deepcopy(_parse_contract(str(path), stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=16)
def load_topology_profile_cached(profile: str) -> Any:
    """Return the deployment topology for ``profile``, parsed once per process.

    The topology model is frozen and its source is a checked-in file, so the one
    parsed instance is safe to share across deliveries and threads. A profile
    that fails to load raises each time: ``lru_cache`` stores no exception.
    """
    from omnibase_infra.topology import load_topology_profile

    return load_topology_profile(profile)
