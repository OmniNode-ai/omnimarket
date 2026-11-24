# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26488: CHANGELOG.md stays removed from the repository root."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_changelog_md_is_absent() -> None:
    assert not (ROOT / "CHANGELOG.md").exists()
