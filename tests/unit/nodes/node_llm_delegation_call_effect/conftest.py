# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane overlay these tests run on binds the host environment (OMN-21595)."""

from __future__ import annotations

import pytest

from tests.overlay_bound_support import host_overlay_fixture

lane_overlay = pytest.fixture(autouse=True)(host_overlay_fixture())
