# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded h101 layout as real git repositories (OMN-22149)."""

from pathlib import Path

import pytest

from tests.helpers.kbi_clone_coverage_layout import Layout, build_layout


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return build_layout(tmp_path)
