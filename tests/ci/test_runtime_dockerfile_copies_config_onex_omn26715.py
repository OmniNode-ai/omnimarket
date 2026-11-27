# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime image copies config/onex/ because pyproject force-includes it (OMN-26715).

Exemplar: tests/ci/test_ticket_contracts_schema_omn17427.py (a repo-file contract read from REPO_ROOT).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_dockerfile_copies_config_onex_before_the_package_install() -> None:
    lines = (REPO_ROOT / "Dockerfile.runtime").read_text().splitlines()
    copies = [i for i, line in enumerate(lines) if "config/onex/" in line and line.startswith("COPY")]
    assert copies, "Dockerfile.runtime has no COPY of config/onex/"
    installs = [i for i, line in enumerate(lines) if "pip install" in line or "uv pip" in line or "uv sync" in line]
    assert not installs or copies[0] < installs[0]
