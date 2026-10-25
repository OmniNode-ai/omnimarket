# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory reads the synthetic overlay tree unless it sets its own."""

from __future__ import annotations

import pytest

from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV


@pytest.fixture(autouse=True)
def _overlay_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
