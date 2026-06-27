# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_aislop_sweep (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): builds synthetic repo trees under ``tmp_path``, drives
``NodeAislopSweep.handle`` in-process, and asserts typed result fields
(``status``, ``total_findings``, ``repos_scanned``, ``by_check``, ``by_severity``).

This is NOT a smoke/assert-no-raise test: every case asserts concrete finding
structure, and the negative-control cases (hardcoded path / prohibited pattern /
TODO marker) require a known-bad fixture to produce a real finding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)


def _write(tree: Path, rel: str, content: str) -> None:
    target = tree / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# Each builder returns the repo-root path to scan; the param tuple carries the
# request kwargs and the expected typed outcome.
def _clean_tree(tmp: Path) -> str:
    _write(tmp, "src/clean.py", "def hello():\n    return 42\n")
    return str(tmp)


def _prohibited_tree(tmp: Path) -> str:
    _write(tmp, "src/bad.py", 'ONEX_EVENT_BUS_TYPE = "inmemory"\n')
    return str(tmp)


def _todo_tree(tmp: Path) -> str:
    _write(tmp, "src/wip.py", "# TODO: fix this later\nx = 1\n")
    return str(tmp)


def _topic_tree(tmp: Path) -> str:
    _write(tmp, "src/topics.py", 'TOPIC = "onex.evt.core.something.v1"\n')
    return str(tmp)


# (builder, request_kwargs, expected_status, expected_min_findings,
#  expected_check_key, expected_check_min)
CASES = [
    pytest.param(
        _clean_tree,
        {"checks": ["prohibited-patterns", "todo-fixme"]},
        "clean",
        0,
        None,
        0,
        id="clean-tree-zero-findings",
    ),
    pytest.param(
        _prohibited_tree,
        {"checks": ["prohibited-patterns"]},
        "findings",
        1,
        "prohibited-patterns",
        1,
        id="prohibited-pattern-negative-control",
    ),
    pytest.param(
        _todo_tree,
        {"checks": ["todo-fixme"]},
        "findings",
        1,
        "todo-fixme",
        1,
        id="todo-marker-negative-control",
    ),
    pytest.param(
        _topic_tree,
        {"checks": ["hardcoded-topics"]},
        "findings",
        1,
        "hardcoded-topics",
        1,
        id="hardcoded-topic-negative-control",
    ),
    pytest.param(
        _topic_tree,
        {"checks": ["todo-fixme"], "dry_run": True},
        "clean",
        0,
        None,
        0,
        id="selective-check-filters-out-topic",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("builder", "kwargs", "expected_status", "min_findings", "check_key", "check_min"),
    CASES,
)
def test_aislop_sweep_multiparam(
    tmp_path: Path,
    integration_event_bus: Any,
    builder: Any,
    kwargs: dict[str, Any],
    expected_status: str,
    min_findings: int,
    check_key: str | None,
    check_min: int,
) -> None:
    target = builder(tmp_path)
    request = AislopSweepRequest(target_dirs=[target], **kwargs)

    result = NodeAislopSweep(event_bus=integration_event_bus).handle(request)

    assert result.status == expected_status
    assert result.repos_scanned == 1
    assert result.total_findings >= min_findings
    assert result.total_findings == len(result.findings)
    assert result.dry_run is bool(kwargs.get("dry_run", False))
    if check_key is not None:
        assert result.by_check.get(check_key, 0) >= check_min
        # Every finding must carry the requested check (selective-check contract).
        assert {f.check for f in result.findings} <= set(kwargs["checks"])
    else:
        assert result.findings == []


@pytest.mark.integration
def test_aislop_sweep_multi_repo_aggregation(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """Scanning multiple repo roots aggregates findings and repo counts."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    for repo in (repo_a, repo_b):
        _write(repo, "src/bad.py", 'ONEX_EVENT_BUS_TYPE = "inmemory"\n')

    result = NodeAislopSweep(event_bus=integration_event_bus).handle(
        AislopSweepRequest(
            target_dirs=[str(repo_a), str(repo_b)],
            checks=["prohibited-patterns"],
        )
    )

    assert result.repos_scanned == 2
    assert result.total_findings == 2
    assert result.status == "findings"
    assert result.by_severity.get("CRITICAL", 0) == 2
