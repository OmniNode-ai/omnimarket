# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSkillFunctionalAuditCompute (OMN-13512 methodology rewrite)."""

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
def test_pure_instruction_skill_is_not_a_gap(tmp_path: Path) -> None:
    """A skill with no backing node and no stateful orchestration is WORKS.

    Phase 3c pure-instruction exemption: skills like using_git_worktrees or
    systematic_debugging intentionally have no backing node. They must not be
    flagged as gaps.
    """
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "using-git-worktrees"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: using_git_worktrees
description: Create isolated git worktrees for feature work.
---

# Using Git Worktrees

Follow these steps to create a worktree.
""",
        encoding="utf-8",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok"
    assert result.gaps == []


@pytest.mark.unit
def test_stateful_orchestration_without_backing_is_facade(tmp_path: Path) -> None:
    """A long SKILL.md describing stateful orchestration with no backing = FACADE."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "pipeline-fill"
    skill_dir.mkdir(parents=True)
    body = "\n".join(f"Step {i} of the orchestration loop." for i in range(220))
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: pipeline_fill
description: RSD-driven continuous pipeline fill.
---

# Pipeline Fill

This skill writes dispatched.yaml state and enforces a wave cap with in-flight
tracking.

{body}
""",
        encoding="utf-8",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "gap"
    assert result.gaps == ["pipeline_fill"]


@pytest.mark.unit
def test_explicit_pure_instruction_marker_overrides_facade(tmp_path: Path) -> None:
    """A long stateful-looking SKILL.md marked instruction-only is not a FACADE."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "executing-plans"
    skill_dir.mkdir(parents=True)
    body = "\n".join(f"Guidance line {i} with wave cap mention." for i in range(220))
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: executing_plans
description: Instruction-only methodology for executing plans.
---

# Executing Plans

This is a pure-instruction skill. The wave cap and state tracking described
below are advisory prose guidance, not deterministic implementation.

{body}
""",
        encoding="utf-8",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok"


@pytest.mark.unit
def test_prose_node_tokens_are_not_extracted_as_backing(tmp_path: Path) -> None:
    """Prose node_* tokens must not be mistaken for a declared backing node.

    The old regex grabbed the first node_* substring anywhere, including
    <node_path>, node_name, node_type. With no DECLARED backing node and no
    stateful orchestration, the skill is WORKS, not a gap pointing at a
    nonexistent node.
    """
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "prose-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: prose_skill
description: A skill whose prose mentions node tokens.
---

# Prose Skill

Run `onex run-node <node_path>` where node_name, node_type, and node_version
are placeholders.
""",
        encoding="utf-8",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok"
    assert result.gaps == []


@pytest.mark.unit
def test_orchestrator_handler_routing_is_recognized(tmp_path: Path) -> None:
    """ORCHESTRATOR nodes route via handler_routing, not handler.module."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    _write_skill(
        skills_root, "merge-sweep", "merge_sweep", "node_pr_lifecycle_orchestrator"
    )

    node_dir = nodes_root / "node_pr_lifecycle_orchestrator"
    handler_dir = node_dir / "handlers"
    handler_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        """---
name: node_pr_lifecycle_orchestrator
node_not_implemented: false
handler_routing:
  start:
    module: omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_start
  finish:
    module: omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_finish
""",
        encoding="utf-8",
    )
    (handler_dir / "handler_start.py").write_text(
        "class HandlerStart: pass\n", encoding="utf-8"
    )
    (handler_dir / "handler_finish.py").write_text(
        "class HandlerFinish: pass\n", encoding="utf-8"
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok", result.verdicts[0].gaps
    assert result.gaps == []


@pytest.mark.unit
def test_handler_routing_stub_marker_detected(tmp_path: Path) -> None:
    """Stub markers inside a handler_routing-routed handler are still caught."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    _write_skill(skills_root, "merge-sweep", "merge_sweep", "node_orch")

    node_dir = nodes_root / "node_orch"
    handler_dir = node_dir / "handlers"
    handler_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        """---
name: node_orch
node_not_implemented: false
handler_routing:
  start:
    module: omnimarket.nodes.node_orch.handlers.handler_start
""",
        encoding="utf-8",
    )
    (handler_dir / "handler_start.py").write_text(
        "raise NotImplementedError('todo')\n", encoding="utf-8"
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "stub"
    assert result.stubs_found == ["merge_sweep"]


@pytest.mark.unit
def test_declared_backing_node_with_bold_form(tmp_path: Path) -> None:
    """The canonical **Backing node**: `node_foo` form is extracted."""
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "pr-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: pr_review
description: PR review skill.
---

# PR Review

**Skill ID**: `onex:pr_review` · **Backing node**: `node_pr_review_orchestrator`
""",
        encoding="utf-8",
    )
    _write_node(
        nodes_root,
        "node_pr_review_orchestrator",
        "handler_pr_review.py",
        "class Handler: pass",
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok", result.verdicts[0].gaps


@pytest.mark.unit
def test_shell_node_with_node_entrypoint_is_wired(tmp_path: Path) -> None:
    """A shell ORCHESTRATOR (node.py + shared handler, no handlers/ dir) is OK.

    omniclaude node_skill_*_orchestrator shells dispatch to a shared handler via
    node.py and a components handler block; they have no local handlers/ dir.
    They must not be flagged as missing handler wiring.
    """
    skills_root = tmp_path / "skills"
    nodes_root = tmp_path / "nodes"
    skill_dir = skills_root / "autopilot"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: autopilot
description: Autopilot dispatch surface.
---

# Autopilot

**Backing node**: `node_skill_autopilot_orchestrator`
""",
        encoding="utf-8",
    )

    node_dir = nodes_root / "node_skill_autopilot_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        """---
name: node_skill_autopilot_orchestrator
node_type: ORCHESTRATOR_GENERIC
node_not_implemented: false
components:
  - name: handler_skill_requested
    type: handler
    function: handle_skill_requested
    module: omniclaude.shared
""",
        encoding="utf-8",
    )
    (node_dir / "node.py").write_text(
        "class NodeSkillAutopilotOrchestrator: pass\n", encoding="utf-8"
    )

    handler = HandlerSkillFunctionalAuditCompute()
    result = handler.handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_roots=[str(skills_root)],
            nodes_root=str(nodes_root),
        )
    )

    assert result.total_audited == 1
    assert result.verdicts[0].status == "ok", result.verdicts[0].gaps
    assert result.gaps == []


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
