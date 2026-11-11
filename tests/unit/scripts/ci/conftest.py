# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate tests read the registry the node resolved from the shipped files (OMN-23505)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci import ci_summary_gate as gate
from tests.unit.scripts.ci.registry_support import shipped_request, write_resolved


@pytest.fixture(autouse=True)
def resolved_registry(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = write_resolved(
        tmp_path_factory.mktemp("registry") / "resolved.json", shipped_request()
    )
    monkeypatch.setattr(gate, "REGISTRY_FILE", path)
    return path
