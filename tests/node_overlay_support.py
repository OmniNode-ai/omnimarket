# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test support: write a node deployment overlay under a temporary root (OMN-20935).

Deployment facts (hosts, endpoints) come from an overlay supplied by whoever runs the
system. A test that needs one writes it under ``tmp_path`` and points
``ONEX_SKILL_OVERLAY_ROOTS`` at that root, using only RFC 5737 documentation addresses
or ``.test`` names.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml


def install_node_overlay(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    node_name: str,
    fields: Mapping[str, object],
) -> Path:
    """Write ``<root>/<node_name>/overlay.yaml`` and make ``root`` the only overlay root."""
    path = root / node_name / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(fields)), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return path


#: The integration sweep's deployment overlay as the suite supplies it.
INTEGRATION_SWEEP_OVERLAY: dict[str, str] = {
    "runtime_host": "192.0.2.10",
    "runtime_repo_path": "/srv/example/omnimarket",
    "stability_test_runtime_url": "http://192.0.2.10:18085",
    "container_health_host": "192.0.2.10",
    "infra_runtime_host": "192.0.2.10",
    "projection_api_url": "http://192.0.2.10:3002",
}
