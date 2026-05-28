# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSkillFunctionalAuditCompute — Stub detection and connectivity checks across skills.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_result import (
    ModelSkillFunctionalAuditComputeResult,
    ModelSkillVerdict,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_NODE_RE = re.compile(r"\bnode_[A-Za-z0-9_]+\b")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*", re.DOTALL)
_STUB_MARKERS = ("NotImplementedError", "node_not_implemented: true", "STUB")


class HandlerSkillFunctionalAuditCompute:
    """Static audit of skill shims against native Onex node contracts."""

    def handle(
        self, request: ModelSkillFunctionalAuditComputeRequest
    ) -> ModelSkillFunctionalAuditComputeResult:
        try:
            verdicts = _audit_skills(request)
        except Exception as exc:
            return ModelSkillFunctionalAuditComputeResult(
                status="error",
                verdicts=[],
                stubs_found=[],
                gaps=[],
                total_audited=0,
                error=str(exc),
            )

        stubs_found = [verdict.name for verdict in verdicts if verdict.stubs_found]
        gaps = [verdict.name for verdict in verdicts if verdict.gaps]
        return ModelSkillFunctionalAuditComputeResult(
            status="ok",
            verdicts=verdicts,
            stubs_found=stubs_found,
            gaps=gaps,
            total_audited=len(verdicts),
            error=None,
        )


@dataclass(frozen=True)
class _SkillShim:
    name: str
    path: Path
    content: str
    node_name: str | None


def _audit_skills(
    request: ModelSkillFunctionalAuditComputeRequest,
) -> list[ModelSkillVerdict]:
    skills = _discover_skills(_resolve_skill_roots(request.skills_roots))
    filters = set(request.skills_filter or ())
    if filters:
        skills = [
            skill
            for skill in skills
            if skill.name in filters
            or skill.path.parent.name in filters
            or f"onex:{skill.path.parent.name}" in filters
            or f"onex:{skill.name}" in filters
        ]

    nodes_root = (
        Path(request.nodes_root) if request.nodes_root else _default_nodes_root()
    )
    return [_audit_skill(skill, nodes_root=nodes_root) for skill in skills]


def _discover_skills(roots: list[Path]) -> list[_SkillShim]:
    skills: list[_SkillShim] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_path in sorted(root.glob("*/SKILL.md")):
            content = skill_path.read_text(encoding="utf-8")
            name = _skill_name(skill_path, content)
            skills.append(
                _SkillShim(
                    name=name,
                    path=skill_path,
                    content=content,
                    node_name=_extract_node_name(content),
                )
            )
    return skills


def _audit_skill(skill: _SkillShim, *, nodes_root: Path) -> ModelSkillVerdict:
    gaps: list[str] = []
    stubs_found: list[str] = []

    if skill.node_name is None:
        gaps.append(f"{skill.path}: missing backing node reference")
        return ModelSkillVerdict(
            name=skill.name,
            status="gap",
            stubs_found=stubs_found,
            gaps=gaps,
        )

    node_dir = nodes_root / skill.node_name
    contract_path = node_dir / "contract.yaml"
    if not contract_path.is_file():
        gaps.append(f"{skill.node_name}: missing contract.yaml")
        return ModelSkillVerdict(
            name=skill.name,
            status="gap",
            stubs_found=stubs_found,
            gaps=gaps,
        )

    contract = _read_yaml_mapping(contract_path)
    if contract.get("node_not_implemented") is True:
        stubs_found.append(f"{contract_path}: node_not_implemented")

    handler_path = _handler_path(contract, nodes_root=nodes_root)
    if handler_path is None:
        gaps.append(f"{skill.node_name}: missing handler.module in contract")
    elif not handler_path.is_file():
        gaps.append(f"{skill.node_name}: handler file not found: {handler_path}")
    else:
        handler_text = handler_path.read_text(encoding="utf-8")
        if any(marker in handler_text for marker in _STUB_MARKERS):
            stubs_found.append(str(handler_path))

    if stubs_found:
        status = "stub"
    elif gaps:
        status = "gap"
    else:
        status = "ok"
    return ModelSkillVerdict(
        name=skill.name,
        status=status,
        stubs_found=stubs_found,
        gaps=gaps,
    )


def _resolve_skill_roots(raw_roots: list[str] | None) -> list[Path]:
    if raw_roots:
        return [Path(root) for root in raw_roots]
    return [
        _REPO_ROOT / "plugins" / "onex" / "skills",
        _REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "skills",
    ]


def _default_nodes_root() -> Path:
    return _REPO_ROOT / "src" / "omnimarket" / "nodes"


def _skill_name(path: Path, content: str) -> str:
    frontmatter = _frontmatter(content)
    name = frontmatter.get("name")
    return str(name) if name else path.parent.name


def _extract_node_name(content: str) -> str | None:
    match = _NODE_RE.search(content)
    return match.group(0) if match else None


def _frontmatter(content: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    raw = yaml.safe_load(match.group("body"))
    return raw if isinstance(raw, dict) else {}


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return raw


def _handler_path(contract: dict[str, Any], *, nodes_root: Path) -> Path | None:
    handler = contract.get("handler")
    if not isinstance(handler, dict):
        return None
    module = handler.get("module")
    if not isinstance(module, str):
        return None
    prefix = "omnimarket.nodes."
    if not module.startswith(prefix):
        return None
    relative = module.removeprefix(prefix)
    return nodes_root / Path(*relative.split(".")).with_suffix(".py")
