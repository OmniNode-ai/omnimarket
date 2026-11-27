# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``--all-nodes`` option of the trigger declaration check: run it over every node contract of the repository."""

from __future__ import annotations

import pytest

ALL_NODES_OPTION = "--all-nodes"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        ALL_NODES_OPTION,
        action="store_true",
        default=False,
        help="check every node contract of the repository for a declared trigger, not only the changed ones",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "all_nodes_run: runs over every node contract of the repository; selected only with --all-nodes",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """The whole-repository test runs only with ``--all-nodes``; without it the test is deselected, never skipped."""
    if config.getoption("all_nodes"):
        return
    kept = [item for item in items if "all_nodes_run" not in item.keywords]
    dropped = [item for item in items if "all_nodes_run" in item.keywords]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
