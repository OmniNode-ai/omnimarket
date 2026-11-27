# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane overlay these tests run on binds the host environment (OMN-21595)."""

from __future__ import annotations

import pytest

from tests.overlay_bound_support import host_overlay_fixture

lane_overlay = pytest.fixture(autouse=True)(host_overlay_fixture())


@pytest.fixture(autouse=True)
def no_ambient_codex_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signed-in-only guard refuses an API key, so no test inherits one."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
