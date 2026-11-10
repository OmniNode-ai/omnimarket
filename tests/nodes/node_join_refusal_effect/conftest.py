# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Markers of the join refusal seam tests (OMN-23503)."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "join_j6_parity: the J6 refusal seam answers the same on the in-memory bus and on a Redpanda bus (needs JOIN_J6_PARITY_BOOTSTRAP for the Redpanda leg)",
    )
