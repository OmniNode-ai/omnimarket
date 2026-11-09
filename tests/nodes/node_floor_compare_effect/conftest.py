# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The comparison tests read no source of the machine they run on: the ledger read command and the landing directory are named by the test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ONEX_LEDGER_BUS_READ_COMMAND", "LANDING_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
