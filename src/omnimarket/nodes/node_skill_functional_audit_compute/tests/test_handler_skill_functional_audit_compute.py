# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSkillFunctionalAuditCompute."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_skill_functional_audit_compute.handlers.handler_skill_functional_audit_compute import (
    HandlerSkillFunctionalAuditCompute,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)


@pytest.mark.unit
def test_handler_detects_ok_stub_and_gap(tmp_path: Path) -> None:
    """Handler audits skill shims against node contracts and handlers."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    _write_skill(skills_root, "ok-skill", "ok_skill", "node_ok_compute")
    _write_skill(skills_root, "stub-skill", "stub_skill", "node_stub_compute")
    _write_skill(skills_root, "gap-skill", "gap_skill", "node_missing_compute")

    _write_node(nodes_root, "node_ok_compute", "handler_ok.py", "class Handler: pass")
    _write_node(
        nodes_root,
        "node_stub_compute",
        "handler_stub.py",
        "raise NotImplementedError('stub')",
        node_not_implemented=True,
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.status == "ok"
    assert result.total_audited == 3
    by_name = {verdict.name: verdict for verdict in result.verdicts}
    assert by_name["ok_skill"].status == "ok"
    assert by_name["stub_skill"].status == "stub"
    assert by_name["gap_skill"].status == "gap"
    assert result.stubs_found == ["stub_skill"]
    assert result.gaps == ["gap_skill"]


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelSkillFunctionalAuditComputeRequest()
    with pytest.raises(ValidationError):
        request.skills_filter = ["some-skill"]  # type: ignore[misc]


@pytest.mark.unit
def test_request_default_skills_filter_is_none() -> None:
    """skills_filter defaults to None (meaning all skills)."""
    request = ModelSkillFunctionalAuditComputeRequest()
    assert request.skills_filter is None


@pytest.mark.unit
def test_request_with_explicit_filter() -> None:
    """skills_filter can be set to a list of skill names."""
    request = ModelSkillFunctionalAuditComputeRequest(
        skills_filter=["onex:aislop_sweep", "onex:contract_sweep"]
    )
    assert request.skills_filter == ["onex:aislop_sweep", "onex:contract_sweep"]


@pytest.mark.unit
def test_handler_applies_skill_filter(tmp_path: Path) -> None:
    """skills_filter limits the audited skill set."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    _write_skill(skills_root, "ok-skill", "ok_skill", "node_ok_compute")
    _write_skill(skills_root, "other-skill", "other_skill", "node_other_compute")
    _write_node(nodes_root, "node_ok_compute", "handler_ok.py", "class Handler: pass")
    _write_node(
        nodes_root,
        "node_other_compute",
        "handler_other.py",
        "class Handler: pass",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_filter=["ok_skill"],
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].name == "ok_skill"


def _write_skill(root: Path, directory: str, name: str, node_name: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Test skill for {node_name}.
---

# {name}

Backing node: `src/omnimarket/nodes/{node_name}/`
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
    handler_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        f"""---
name: {node_name}
node_not_implemented: {str(node_not_implemented).lower()}
handler:
  module: omnimarket.nodes.{node_name}.handlers.{handler_filename.removesuffix(".py")}
""",
        encoding="utf-8",
    )
    (handler_dir / handler_filename).write_text(handler_content, encoding="utf-8")
