# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of node_code_contract_score_effect tests (OMN-23626)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    Fixture,
    build_repo,
)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build_repo(tmp_path)
