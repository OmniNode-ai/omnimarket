# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_coverage_sweep (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): writes synthetic ``coverage.json`` files under repo roots
in ``tmp_path`` (passed as explicit ``target_dirs``), drives
``NodeCoverageSweep.handle`` in-process, and asserts typed result fields
(``status``, ``total_modules``, ``below_target``, ``zero_coverage``,
``average_coverage``, ``by_priority``).

Negative controls: a below-target module produces a ``BELOW_TARGET`` gap and a
zero-coverage module produces a ``ZERO`` gap; ``recently_changed_modules``
re-prioritises a below-target module to ``RECENTLY_CHANGED``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    CoverageSweepRequest,
    NodeCoverageSweep,
)


def _coverage(modules: dict[str, float]) -> dict[str, Any]:
    """Build a coverage.json-shaped dict from {module_path: percent_covered}."""
    return {
        "files": {
            mod: {
                "summary": {
                    "percent_covered": pct,
                    "num_statements": 10,
                    "missing_lines": round((100 - pct) / 10),
                }
            }
            for mod, pct in modules.items()
        }
    }


def _write_repo(root: Path, name: str, modules: dict[str, float]) -> str:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "coverage.json").write_text(
        json.dumps(_coverage(modules)), encoding="utf-8"
    )
    return str(repo)


# (modules map, target_pct, recently_changed, expected_status, expected_below,
#  expected_zero, required_priority|None)
CASES = [
    pytest.param(
        {"src/a.py": 90.0, "src/b.py": 80.0},
        50.0,
        [],
        "clean",
        0,
        0,
        None,
        id="all-above-target-clean",
    ),
    pytest.param(
        {"src/a.py": 90.0, "src/b.py": 20.0},
        50.0,
        [],
        "gaps_found",
        1,
        0,
        "BELOW_TARGET",
        id="below-target-negative-control",
    ),
    pytest.param(
        {"src/a.py": 90.0, "src/c.py": 0.0},
        50.0,
        [],
        "gaps_found",
        1,
        1,
        "ZERO",
        id="zero-coverage-negative-control",
    ),
    pytest.param(
        # target_pct boundary: 50.0 is NOT < 50.0, so the 50% module is clean.
        {"src/a.py": 50.0},
        50.0,
        [],
        "clean",
        0,
        0,
        None,
        id="target-pct-boundary-exclusive",
    ),
    pytest.param(
        {"src/a.py": 90.0, "src/b.py": 20.0},
        50.0,
        ["src/b.py"],
        "gaps_found",
        1,
        0,
        "RECENTLY_CHANGED",
        id="recently-changed-reprioritises",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "modules",
        "target_pct",
        "recent",
        "expected_status",
        "exp_below",
        "exp_zero",
        "priority",
    ),
    CASES,
)
def test_coverage_sweep_multiparam(
    tmp_path: Path,
    modules: dict[str, float],
    target_pct: float,
    recent: list[str],
    expected_status: str,
    exp_below: int,
    exp_zero: int,
    priority: str | None,
) -> None:
    target = _write_repo(tmp_path, "myrepo", modules)
    request = CoverageSweepRequest(
        target_dirs=[target],
        target_pct=target_pct,
        recently_changed_modules=recent,
    )

    result = NodeCoverageSweep().handle(request)

    assert result.status == expected_status
    assert result.repos_scanned == 1
    assert result.total_modules == len(modules)
    assert result.below_target == exp_below
    assert result.zero_coverage == exp_zero
    assert result.total_gaps == len(result.gaps)
    if priority is not None:
        assert result.by_priority.get(priority, 0) >= 1
        assert all(g.repo == "myrepo" for g in result.gaps)
        assert all(g.coverage_pct < target_pct for g in result.gaps)
    else:
        assert result.gaps == []


@pytest.mark.integration
def test_coverage_sweep_multi_repo_average(tmp_path: Path) -> None:
    """Two repos: aggregate module count, gaps, and the average-coverage field."""
    repo_a = _write_repo(tmp_path, "repo_a", {"src/a.py": 100.0})
    repo_b = _write_repo(tmp_path, "repo_b", {"src/b.py": 0.0})

    result = NodeCoverageSweep().handle(
        CoverageSweepRequest(target_dirs=[repo_a, repo_b], target_pct=50.0)
    )

    assert result.repos_scanned == 2
    assert result.total_modules == 2
    assert result.zero_coverage == 1
    assert result.average_coverage == pytest.approx(50.0)
    assert {g.repo for g in result.gaps} == {"repo_b"}
