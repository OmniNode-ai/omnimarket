# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_feature_dashboard_compute.

WS-5 Wave 8 (OMN-13682). Variant A — the COMPUTE handler is driven in-process
against a *synthetic repo tree* built under ``tmp_path`` (SKILL.md docs + backing
``node_*`` dirs + contract.yaml + handler/models/tests + pyproject). Each case
varies the skill filter, the requested layer checks, and which skills are present
(fully-wired vs gap), and asserts the typed ``ModelFeatureDashboardResult``
(status, gaps, per-skill coverage scores, checks_run).

Negative control: a deliberately under-wired skill (missing handler module) must
surface a HIGH-severity ``handler`` gap and flip the overall status to
``partial``. A run that reported ``complete`` over the broken skill would be a
regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_feature_dashboard_compute.handlers.handler_feature_dashboard_compute import (
    HandlerFeatureDashboardCompute,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    ModelFeatureDashboardRequest,
)


def _write_full_node(repo_root: Path, node_name: str) -> None:
    node_dir = repo_root / "src" / "omnimarket" / "nodes" / node_name
    (node_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (node_dir / "models").mkdir(parents=True, exist_ok=True)
    (node_dir / "tests").mkdir(parents=True, exist_ok=True)
    module = f"omnimarket.nodes.{node_name}.handlers.handler_{node_name.removeprefix('node_')}"
    (node_dir / "contract.yaml").write_text(
        f"""---
name: {node_name}
node_not_implemented: false
handler:
  module: {module}
event_bus:
  publish_topics:
    - onex.evt.{node_name}.done.v1
  subscribe_topics:
    - onex.cmd.{node_name}.start.v1
""",
        encoding="utf-8",
    )
    (
        node_dir / "handlers" / f"handler_{node_name.removeprefix('node_')}.py"
    ).write_text("class Handler: pass\n", encoding="utf-8")
    (node_dir / "models" / "model_x.py").write_text("X = 1\n", encoding="utf-8")
    (node_dir / "tests" / "__init__.py").write_text("", encoding="utf-8")


def _write_broken_node(repo_root: Path, node_name: str) -> None:
    """Backing node present, but no handler module declared → handler-layer gap."""
    node_dir = repo_root / "src" / "omnimarket" / "nodes" / node_name
    (node_dir / "models").mkdir(parents=True, exist_ok=True)
    (node_dir / "tests").mkdir(parents=True, exist_ok=True)
    (node_dir / "contract.yaml").write_text(
        f"""---
name: {node_name}
node_not_implemented: false
event_bus:
  publish_topics:
    - onex.evt.{node_name}.done.v1
  subscribe_topics:
    - onex.cmd.{node_name}.start.v1
""",
        encoding="utf-8",
    )
    (node_dir / "models" / "model_x.py").write_text("X = 1\n", encoding="utf-8")
    (node_dir / "tests" / "__init__.py").write_text("", encoding="utf-8")


def _write_skill(repo_root: Path, skill: str, node_name: str) -> None:
    skill_dir = repo_root / "plugins" / "onex" / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {skill}
description: Synthetic skill for {node_name}.
---

# {skill}

Backing node: `src/omnimarket/nodes/{node_name}/`
Command name: `{skill}`
""",
        encoding="utf-8",
    )


def _build_repo(tmp_path: Path, *, include_good: bool, include_broken: bool) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "plugins" / "onex" / "skills").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "omnimarket" / "nodes").mkdir(parents=True, exist_ok=True)
    declared_nodes: list[str] = []
    if include_good:
        _write_skill(repo_root, "good", "node_good")
        _write_full_node(repo_root, "node_good")
        declared_nodes.append("node_good")
    if include_broken:
        _write_skill(repo_root, "broken", "node_broken")
        _write_broken_node(repo_root, "node_broken")
        declared_nodes.append("node_broken")
    # entry_point layer reads pyproject.toml for the node name.
    entries = "\n".join(f'{n} = "omnimarket.nodes.{n}"' for n in declared_nodes)
    (repo_root / "pyproject.toml").write_text(
        f'[project.entry-points."onex.nodes"]\n{entries}\n', encoding="utf-8"
    )
    return repo_root


# (include_good, include_broken, skills_filter, check_types, expected_status,
#  expected_audited, expect_handler_gap, expect_good_complete)
_CASES = [
    pytest.param(
        True,
        True,
        None,
        None,
        "partial",
        2,
        True,
        True,
        id="good-plus-broken-partial",
    ),
    pytest.param(
        True,
        False,
        ["good"],
        None,
        "complete",
        1,
        False,
        True,
        id="good-only-complete",
    ),
    pytest.param(
        True,
        True,
        ["broken"],
        None,
        "partial",
        1,
        True,
        False,
        id="broken-only-handler-gap-negative-control",
    ),
    pytest.param(
        True,
        False,
        ["good"],
        ["skill_doc", "backing_node"],
        "complete",
        1,
        False,
        False,
        id="check-types-subset",
    ),
    pytest.param(
        False,
        False,
        None,
        None,
        "empty",
        0,
        False,
        False,
        id="empty-repo-no-skills",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "include_good",
        "include_broken",
        "skills_filter",
        "check_types",
        "expected_status",
        "expected_audited",
        "expect_handler_gap",
        "expect_good_complete",
    ),
    _CASES,
)
def test_feature_dashboard_multiparam(
    tmp_path: Path,
    include_good: bool,
    include_broken: bool,
    skills_filter: list[str] | None,
    check_types: list[str] | None,
    expected_status: str,
    expected_audited: int,
    expect_handler_gap: bool,
    expect_good_complete: bool,
) -> None:
    repo_root = _build_repo(
        tmp_path, include_good=include_good, include_broken=include_broken
    )

    result = HandlerFeatureDashboardCompute().handle(
        ModelFeatureDashboardRequest(
            skills=skills_filter,
            check_types=check_types,
            repo_root=str(repo_root),
        )
    )

    assert result.status == expected_status
    assert result.skills_audited == expected_audited
    assert len(result.coverage_report) == expected_audited

    # checks_run reflects the requested subset (or all 8 when None).
    if check_types is not None:
        assert result.checks_run == check_types
        for coverage in result.coverage_report.values():
            assert set(coverage["checks"]) == set(check_types)  # type: ignore[index]

    # Negative control: the broken skill yields a HIGH-severity handler gap.
    if expect_handler_gap:
        handler_gaps = [
            gap
            for gap in result.gaps
            if gap["check_type"] == "handler" and gap["skill"] == "broken"
        ]
        assert handler_gaps, result.gaps
        assert handler_gaps[0]["severity"] == "HIGH"
    elif expected_status == "complete":
        assert result.gaps == []

    # Fully-wired skill scores a perfect coverage map.
    if expect_good_complete:
        good = result.coverage_report["good"]
        assert good["coverage_score"] == 1.0, good
        assert all(good["checks"].values())  # type: ignore[union-attr]
