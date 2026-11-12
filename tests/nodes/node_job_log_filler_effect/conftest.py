# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every filler test writes its red-facts records under its own state directory (OMN-23832)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def planted_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANTED_STATE_DIR", str(tmp_path / "planted-state"))
