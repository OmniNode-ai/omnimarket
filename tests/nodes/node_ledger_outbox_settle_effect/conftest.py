# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the outbox settle tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("", encoding="utf-8")
    return path
