# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_skill_functional_audit_compute.

WS-5 Wave 8 (OMN-13682). Variant A — the COMPUTE handler is driven in-process
against a *synthetic skills/nodes tree* built under ``tmp_path``. Every case
exercises a distinct slice of the audit methodology (ok/stub/gap mix,
pure-instruction exemption, FACADE detection, skills-filter, backing-node
resolution) and asserts the typed ``ModelSkillFunctionalAuditComputeResult``
fields — NOT merely that the handler returned.

Negative controls: the synthetic tree intentionally seeds a stub-marker handler,
a missing-backing-node skill, and a stateful-orchestration FACADE. Every full
(unfiltered) audit must surface those as ``stub`` / ``gap`` verdicts; a run that
produced zero findings over this tree would be a regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_skill_functional_audit_compute.handlers.handler_skill_functional_audit_compute import (
    HandlerSkillFunctionalAuditCompute,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)

_FACADE_BODY = "\n".join(f"Step {i} of the orchestration loop." for i in range(220))


def _write_backed_skill(root: Path, directory: str, name: str, node_name: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Test skill for {node_name}.
---

# {name}

**Backing node**: `src/omnimarket/nodes/{node_name}/`
""",
        encoding="utf-8",
    )


def _write_node(
    root: Path,
    node_name: str,
    handler_filename: str,
    handler_content: str,
    *,
    node_not_implemented: bool = False,
) -> None:
    node_dir = root / node_name
    handler_dir = node_dir / "handlers"
    handler_dir.mkdir(parents=True, exist_ok=True)
    module = (
        f"omnimarket.nodes.{node_name}.handlers.{handler_filename.removesuffix('.py')}"
    )
    (node_dir / "contract.yaml").write_text(
        f"""---
name: {node_name}
node_not_implemented: {str(node_not_implemented).lower()}
handler:
  module: {module}
""",
        encoding="utf-8",
    )
    (handler_dir / handler_filename).write_text(handler_content, encoding="utf-8")


def _build_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Construct a synthetic skills/nodes tree exercising every verdict path."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"

    # ok — real backing node + clean handler.
    _write_backed_skill(skills_root, "ok-skill", "ok_skill", "node_ok_compute")
    _write_node(nodes_root, "node_ok_compute", "handler_ok.py", "class Handler: pass\n")

    # stub — backing node handler still carries a stub marker.
    _write_backed_skill(skills_root, "stub-skill", "stub_skill", "node_stub_compute")
    _write_node(
        nodes_root,
        "node_stub_compute",
        "handler_stub.py",
        "raise NotImplementedError('stub')\n",
        node_not_implemented=True,
    )

    # gap — skill declares a backing node that does not exist on disk.
    _write_backed_skill(skills_root, "gap-skill", "gap_skill", "node_missing_compute")

    # pure-instruction — no backing node, short, not stateful → exempt (ok).
    pure_dir = skills_root / "using-git-worktrees"
    pure_dir.mkdir(parents=True, exist_ok=True)
    (pure_dir / "SKILL.md").write_text(
        """---
name: pure_skill
description: Create isolated git worktrees for feature work.
---

# Using Git Worktrees

Follow these steps to create a worktree.
""",
        encoding="utf-8",
    )

    # facade — long SKILL.md describing stateful orchestration, no backing node.
    facade_dir = skills_root / "facade-skill"
    facade_dir.mkdir(parents=True, exist_ok=True)
    (facade_dir / "SKILL.md").write_text(
        f"""---
name: facade_skill
description: RSD-driven continuous pipeline fill.
---

# Facade Skill

This skill writes dispatched.yaml state and enforces a wave cap with in-flight
tracking.

{_FACADE_BODY}
""",
        encoding="utf-8",
    )

    return skills_root, nodes_root


# Each case: (skills_filter, expected status-by-skill, expect_stub_names, expect_gap_names)
_CASES = [
    pytest.param(
        None,
        {
            "ok_skill": "ok",
            "stub_skill": "stub",
            "gap_skill": "gap",
            "pure_skill": "ok",
            "facade_skill": "gap",
        },
        {"stub_skill"},
        {"gap_skill", "facade_skill"},
        id="full-audit-ok-stub-gap-pure-facade",
    ),
    pytest.param(
        ["ok_skill"],
        {"ok_skill": "ok"},
        set(),
        set(),
        id="filter-ok-only",
    ),
    pytest.param(
        ["stub_skill"],
        {"stub_skill": "stub"},
        {"stub_skill"},
        set(),
        id="filter-stub-only-negative-control",
    ),
    pytest.param(
        ["gap_skill"],
        {"gap_skill": "gap"},
        set(),
        {"gap_skill"},
        id="filter-backing-node-not-found",
    ),
    pytest.param(
        ["facade_skill"],
        {"facade_skill": "gap"},
        set(),
        {"facade_skill"},
        id="filter-facade-detection",
    ),
    pytest.param(
        ["pure_skill"],
        {"pure_skill": "ok"},
        set(),
        set(),
        id="filter-pure-instruction-exemption",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("skills_filter", "expected_status", "expect_stubs", "expect_gaps"), _CASES
)
def test_skill_functional_audit_multiparam(
    tmp_path: Path,
    skills_filter: list[str] | None,
    expected_status: dict[str, str],
    expect_stubs: set[str],
    expect_gaps: set[str],
) -> None:
    skills_root, nodes_root = _build_tree(tmp_path)

    result = HandlerSkillFunctionalAuditCompute().handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_filter=skills_filter,
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.status == "ok"
    assert result.error is None
    assert result.total_audited == len(expected_status)

    by_name = {verdict.name: verdict for verdict in result.verdicts}
    assert set(by_name) == set(expected_status)
    for name, status in expected_status.items():
        assert by_name[name].status == status, by_name[name].gaps

    # Verdict structural truth: status enum and finding-list coherence.
    for verdict in result.verdicts:
        assert verdict.status in {"ok", "stub", "gap", "error"}
        if verdict.status == "stub":
            assert verdict.stubs_found
        if verdict.status == "gap":
            assert verdict.gaps

    assert set(result.stubs_found) == expect_stubs
    assert set(result.gaps) == expect_gaps


@pytest.mark.integration
def test_full_audit_surfaces_seeded_negative_controls(tmp_path: Path) -> None:
    """The seeded stub/gap/facade fixtures must each yield a finding (no silent pass)."""
    skills_root, nodes_root = _build_tree(tmp_path)

    result = HandlerSkillFunctionalAuditCompute().handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    # A clean tree would produce zero stubs and zero gaps — prove the path runs.
    assert result.stubs_found, "stub-marker handler was not detected"
    assert result.gaps, "missing-node + FACADE skills were not detected"
    facade = next(v for v in result.verdicts if v.name == "facade_skill")
    assert "FACADE" in " ".join(facade.gaps)
