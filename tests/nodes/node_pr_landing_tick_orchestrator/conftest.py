# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's tests run as one named host: the consumer group carries its identity (OMN-22921)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _host_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "test-host")
