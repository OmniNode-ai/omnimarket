# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-root resolution coverage for node_skill_functional_audit_compute.

OMN-13993 (WS-M dogfood rail). The audit must run from the canonical infra
venv, where ``_REPO_ROOT`` (``Path(__file__).parents[5]``) is the site-packages
install dir rather than the omni_home registry checkout. Before the fix, the
default skill roots were resolved relative to ``_REPO_ROOT`` and therefore
pointed at ``<venv>/.../plugins/onex/skills`` — which never exists — so the
audit hard-failed with ``SkillFunctionalAuditNoSkillsDiscoveredError`` from the
infra venv. The fix resolves the skill roots via the ``$OMNI_HOME`` sibling-repo
layout (mirroring ``_resolve_nodes_roots``).

Two behavioral guarantees are proven here:

1. Site-packages ``_REPO_ROOT`` + ``$OMNI_HOME`` set → the audit discovers and
   resolves the real sweep skills (``integration_sweep`` and ``dod_sweep``)
   living in the sibling ``omniclaude/plugins/onex/skills`` clone.
2. Fail-fast (CLAUDE.md rule #8): ``$OMNI_HOME`` unset AND no repo-relative path
   resolves → the audit still raises the zero-skills hard fail. No silent
   wrong-default pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_skill_functional_audit_compute.handlers import (
    handler_skill_functional_audit_compute as handler_mod,
)
from omnimarket.nodes.node_skill_functional_audit_compute.handlers.handler_skill_functional_audit_compute import (
    HandlerSkillFunctionalAuditCompute,
    SkillFunctionalAuditNoSkillsDiscoveredError,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)


def _write_sweep_skill(
    skills_root: Path, skill_dir: str, name: str, node_name: str
) -> None:
    """Write a SKILL.md declaring a canonical backing node."""
    directory = skills_root / skill_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: {name}
description: Contract-driven sweep for {node_name}.
---

# {name}

**Backing node**: `{node_name}`
""",
        encoding="utf-8",
    )


def _write_backing_node(nodes_root: Path, node_name: str) -> None:
    """Write a wired backing node (contract + clean handler) under nodes_root."""
    node_dir = nodes_root / node_name
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True, exist_ok=True)
    module = f"omnimarket.nodes.{node_name}.handlers.handler_{node_name}"
    (node_dir / "contract.yaml").write_text(
        f"""---
name: {node_name}
node_not_implemented: false
handler:
  module: {module}
""",
        encoding="utf-8",
    )
    (handlers_dir / f"handler_{node_name}.py").write_text(
        "class Handler:\n    def handle(self, request):\n        return request\n",
        encoding="utf-8",
    )


def _make_site_packages_repo_root(tmp_path: Path) -> Path:
    """A bare site-packages-style install dir with no plugins/onex/skills."""
    repo_root = tmp_path / "site-packages" / "omnimarket"
    repo_root.mkdir(parents=True, exist_ok=True)
    return repo_root


@pytest.mark.integration
def test_resolves_sweep_skills_via_omni_home_from_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site-packages _REPO_ROOT + $OMNI_HOME → both sweeps resolve (not a hard fail).

    This is the infra-venv scenario: the repo-relative default roots do not
    exist, so resolution must come from the $OMNI_HOME sibling-repo layout. Both
    representative sweep skills — integration_sweep and dod_sweep — must be
    discovered and resolve their backing nodes to STATIC_OK.
    """
    # _REPO_ROOT looks like a site-packages install: no plugins/onex/skills.
    repo_root = _make_site_packages_repo_root(tmp_path)
    monkeypatch.setattr(handler_mod, "_REPO_ROOT", repo_root)

    # Canonical skill library lives in the sibling omniclaude clone under $OMNI_HOME.
    omni_home = tmp_path / "omni_home"
    skills_root = omni_home / "omniclaude" / "plugins" / "onex" / "skills"
    _write_sweep_skill(
        skills_root,
        "integration_sweep",
        "integration_sweep",
        "node_integration_sweep_orchestrator",
    )
    _write_sweep_skill(
        skills_root, "dod_sweep", "dod_sweep", "node_dod_sweep_orchestrator"
    )
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    # Backing nodes for both sweeps resolve from an explicit nodes root.
    nodes_root = tmp_path / "nodes"
    _write_backing_node(nodes_root, "node_integration_sweep_orchestrator")
    _write_backing_node(nodes_root, "node_dod_sweep_orchestrator")

    result = HandlerSkillFunctionalAuditCompute().handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_filter=["integration_sweep", "dod_sweep"],
            skills_roots=None,  # exercise the fixed default resolution
            nodes_root=str(nodes_root),
        )
    )

    assert result.status == "ok"
    assert result.error is None
    by_name = {verdict.name: verdict for verdict in result.verdicts}
    # Both representative sweeps resolved from the sibling repo via $OMNI_HOME.
    assert set(by_name) == {"integration_sweep", "dod_sweep"}
    for name in ("integration_sweep", "dod_sweep"):
        assert by_name[name].status == "STATIC_OK", by_name[name].gaps
        assert not by_name[name].gaps
    assert result.gaps == []
    assert result.stubs_found == []


@pytest.mark.integration
def test_default_roots_include_omni_home_sibling_skill_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved default roots include the OMNI_HOME sibling skill library."""
    repo_root = _make_site_packages_repo_root(tmp_path)
    monkeypatch.setattr(handler_mod, "_REPO_ROOT", repo_root)
    omni_home = tmp_path / "omni_home"
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    roots = handler_mod._resolve_skill_roots(None)

    expected = omni_home / "omniclaude" / "plugins" / "onex" / "skills"
    assert expected in roots
    # The stale site-packages-relative path is still listed (harmless: skipped
    # by the root.exists() guard) but must not be the sole resolution anchor.
    assert roots[0] == expected


@pytest.mark.integration
def test_fail_fast_when_omni_home_unset_and_no_path_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md rule #8: unset $OMNI_HOME + no valid path still raises.

    A site-packages _REPO_ROOT whose parent has no sibling omniclaude clone,
    with $OMNI_HOME unset, must NOT silently pass — it must raise the
    zero-skills hard fail rather than pick up a wrong default directory.
    """
    repo_root = _make_site_packages_repo_root(tmp_path)
    monkeypatch.setattr(handler_mod, "_REPO_ROOT", repo_root)
    monkeypatch.delenv("OMNI_HOME", raising=False)

    # _resolve_omni_home must return None (no env var, no sibling omniclaude).
    assert handler_mod._resolve_omni_home() is None

    with pytest.raises(SkillFunctionalAuditNoSkillsDiscoveredError):
        HandlerSkillFunctionalAuditCompute().handle(
            ModelSkillFunctionalAuditComputeRequest(
                skills_filter=None,
                skills_roots=None,
                nodes_root=None,
            )
        )


@pytest.mark.integration
def test_canonical_clone_fallback_resolves_without_omni_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With $OMNI_HOME unset, a canonical-clone _REPO_ROOT.parent still resolves.

    Running from the registry checkout (not a venv) must keep working even when
    the env var is unset: _REPO_ROOT.parent contains the sibling omniclaude
    clone, so that anchor is honored — but only because it validates on disk,
    never as a blind default.
    """
    omni_home = tmp_path / "omni_home"
    repo_root = omni_home / "omnimarket"
    repo_root.mkdir(parents=True, exist_ok=True)
    skills_root = omni_home / "omniclaude" / "plugins" / "onex" / "skills"
    _write_sweep_skill(
        skills_root,
        "integration_sweep",
        "integration_sweep",
        "node_integration_sweep_orchestrator",
    )
    monkeypatch.setattr(handler_mod, "_REPO_ROOT", repo_root)
    monkeypatch.delenv("OMNI_HOME", raising=False)

    assert handler_mod._resolve_omni_home() == omni_home

    nodes_root = tmp_path / "nodes"
    _write_backing_node(nodes_root, "node_integration_sweep_orchestrator")

    result = HandlerSkillFunctionalAuditCompute().handle(
        ModelSkillFunctionalAuditComputeRequest(
            skills_filter=["integration_sweep"],
            skills_roots=None,
            nodes_root=str(nodes_root),
        )
    )

    assert result.status == "ok"
    by_name = {verdict.name: verdict for verdict in result.verdicts}
    assert by_name["integration_sweep"].status == "STATIC_OK"
