# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shipped generic paths hold files every change touches, never a repository's own ledger file."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    load_generic_paths,
)

pytestmark = pytest.mark.unit


def test_no_generic_path_names_a_ledger_file() -> None:
    assert [p for p in load_generic_paths() if "LEDGER" in p.upper()] == []


def test_the_files_every_change_touches_stay_generic() -> None:
    paths = load_generic_paths()
    assert {"*.lock", "package-lock.json", "CHANGELOG*", "contracts/OMN-*.yaml"} <= set(
        paths
    )
