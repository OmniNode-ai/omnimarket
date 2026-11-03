# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test of this suite runs under both profiles: the in-memory binding and the
Redpanda overlay binding. The profile id is appended to each test id, so the suite shows
one ``[inmemory]`` and one ``[redpanda_overlay]`` line per test function."""

from __future__ import annotations

import pytest

PROFILES = ("inmemory", "redpanda_overlay")


@pytest.fixture(params=PROFILES, autouse=True)
def profile(request: pytest.FixtureRequest) -> str:
    return str(request.param)
