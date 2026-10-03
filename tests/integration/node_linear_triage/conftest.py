# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures for the linear-triage tests."""

from __future__ import annotations

import pytest

from tests.helpers.linear_triage_done_write_gate import stand_down_done_write_gate


@pytest.fixture(autouse=True)
def _done_write_gate_stood_down(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_down_done_write_gate(request, monkeypatch)
