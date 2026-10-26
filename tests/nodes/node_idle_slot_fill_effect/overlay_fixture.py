# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A synthetic overlay tree for node_idle_slot_fill_effect tests, with neutral names only."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPOS: dict[str, str] = {
    "private_data": "repo-private-data",
    "plugin_source": "repo-plugin-source",
    "deploy": "repo-deploy",
    "knowledge_base": "repo-knowledge-base",
}

CLOUD_BACKLOG: dict[str, Any] = {
    "ruling": "RULING 2026-10-21T21:14:58Z: backlog tickets go to free cloud slots (dated paraphrase).",
    "repos": [
        "omnibase_core",
        "omnibase_spi",
        "omnibase_infra",
        "omnimarket",
        "omniclaude",
        "repo-plugin-source",
        "repo-private-data",
        "omniintelligence",
        "omnimemory",
    ],
    "excluded_repos": [
        "repo-deploy",
        "repo-knowledge-base",
        "repo-knowledge-base-hyphen",
        "omnidash",
        "repo-notes-a",
        "repo-notes-b",
    ],
    "lab_markers": [
        "lab host",
        "on the lab",
        "192.0.2.7",
        "host-a",
        "redpanda",
        "kafka broker",
        "kubectl",
        "k8s",
        "helm",
        "deploy",
        "migration",
        "hotpatch",
        "dev lane",
        "launchd",
        "systemd",
        "credential",
        "secret",
        "dashboard",
        "readback",
        "experiment",
        ".github",
        "workflow",
        "workflows",
        ".yml",
    ],
    "sprint_state_types": ["unstarted", "backlog"],
    "max_estimate": 3,
    "window_hours": 24,
}

EXPERIMENT_DESIGNS: list[dict[str, str]] = [
    {
        "id": "design-a",
        "title": "Design A",
        "repo": "repo-plugin-source",
        "design": "Run the same task through two routes and compare the checks passed.",
        "measure": "Record the checks passed for each route",
    },
    {
        "id": "design-b",
        "title": "Design B",
        "repo": "omnimarket",
        "design": "Run the same recorded inputs twice and compare the answers.",
        "measure": "Record the number of differing answers",
    },
]

IDLE_SLOT_FILL: dict[str, Any] = {
    "process_fix_roles": ["private_data", "plugin_source"],
    "mvp_repos": [
        "omnibase_infra",
        "omnibase_core",
        "omnimarket",
        "omniclaude",
        "omnidash",
    ],
    "always_out_roles": ["deploy"],
    "cloud_backlog": CLOUD_BACKLOG,
    "experiment_designs": EXPERIMENT_DESIGNS,
}


def write_overlay(
    root: Path,
    *,
    repos: dict[str, str] | None = REPOS,
    idle_slot_fill: dict[str, Any] | None = IDLE_SLOT_FILL,
) -> dict[str, str]:
    """Write the overlay files under ``root`` and return the environment that names it."""
    for overlay_id, document in (("repos", repos), ("idle_slot_fill", idle_slot_fill)):
        if document is None:
            continue
        path = root / overlay_id / "overlay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


_ROOT = Path(tempfile.mkdtemp(prefix="idle-slot-fill-overlay-"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
ENV: dict[str, str] = write_overlay(_ROOT)
