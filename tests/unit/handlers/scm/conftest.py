# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate-fix guard (OMN-17427) reads the PR watcher, the ledger and git; these suites test other verbs."""

from __future__ import annotations

import pytest

from omnimarket.handlers.scm import scm_cli


@pytest.fixture(autouse=True)
def _pr_create_finds_no_duplicate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every scm suite but the guard's own runs ``pr-create`` with the duplicate check answered clear."""
    if "duplicate_guard" in request.module.__name__:
        return
    monkeypatch.setattr(scm_cli, "refuse_or_continue", lambda **_: None)
