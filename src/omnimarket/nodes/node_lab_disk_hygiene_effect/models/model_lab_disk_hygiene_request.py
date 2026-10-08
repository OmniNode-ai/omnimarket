# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the lab disk hygiene handler (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLabDiskHygieneRequest:
    """One Docker hygiene pass on the host this runs on.

    omni_home is the registry whose volume is measured; census is the lane manifest whose
    compose projects and container names are never touched. A deployment overlay may
    supply the census path when the request omits it. dry_run lists the containers that
    would go and runs no prune. tmp_dirs are swept for stale entries this user owns.
    """

    omni_home: Path
    census: Path | None = None
    build_cache_max_gb: float = 10.0
    image_unused_hours: int = 48
    exited_container_hours: int = 24
    prune_anonymous_volumes: bool = True
    tmp_dirs: tuple[Path, ...] = ()
    tmp_stale_hours: int = 24
    dry_run: bool = False
