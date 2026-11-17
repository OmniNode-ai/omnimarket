# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enable the lab SLI pack for the schedule tests."""

from __future__ import annotations

import pytest

from omnimarket.handlers.lab_sli_pack_gate import LAB_SLI_PACK_OVERLAY
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV


@pytest.fixture(autouse=True)
def _enable_lab_sli_pack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the overlay roots at a pack overlay that enables the lab SLI pack."""
    pack_dir = tmp_path / LAB_SLI_PACK_OVERLAY
    pack_dir.mkdir()
    (pack_dir / "overlay.yaml").write_text("status: enabled\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
