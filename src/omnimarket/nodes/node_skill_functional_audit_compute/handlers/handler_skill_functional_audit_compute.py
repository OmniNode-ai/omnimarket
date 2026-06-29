# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSkillFunctionalAuditCompute — facade/stub detection across onex skills.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.

This handler implements the ``skill_functional_audit`` SKILL.md methodology
(Phase 2 risk-tier classification, Phase 3c pure-instruction exemption,
orchestrator ``handler_routing`` support, resilient backing-node extraction).

Why the rewrite (OMN-13512): the prior heuristics were far cruder than the
methodology they claimed to implement and produced systematic false positives:

* A skill with no ``node_*`` token was unconditionally flagged a gap, ignoring
  the Phase 3c pure-instruction exemption (using_git_worktrees,
  systematic_debugging, writing_skills, login, rewind, ...).
* ``_extract_node_name`` grabbed the FIRST ``node_*`` substring anywhere in the
  SKILL.md — including prose like ``<node_path>``, ``node_name``, ``node_type``.
* ``_handler_path`` read only ``contract["handler"]["module"]``; canonical
  ORCHESTRATOR nodes route via a ``handler_routing`` map and have no top-level
  ``handler.module``, so real wired nodes were flagged "missing handler.module".
* Backing nodes were resolved only under a single omnimarket nodes root; skills
  backed by omniclaude shell nodes (node_skill_*_orchestrator) could not be
  found and were flagged drifted.

The resilient backing-node extractor mirrors the canonical declaration form
enforced by ``omnibase_core.validation.validator_skill_backing_node`` so the
audit and the liveness gate agree on what a "declared backing node" is.
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
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*", re.DOTALL)
_STUB_MARKERS = ("NotImplementedError", "node_not_implemented: true", "STUB")

# Backing-node declaration forms, priority order; first match per file wins.
# Mirrors omnibase_core.validation.validator_skill_backing_node so the audit and
# the liveness gate agree on what counts as a *declared* backing node. Prose
# tokens like ``<node_path>`` / ``node_name`` are NOT matched — only an explicit
# "Backing node" field or ``backing_node:`` frontmatter key.
_BACKING_NODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Canonical body form: **Backing node**: `omnimarket/.../node_foo/`
    re.compile(
        r"\*\*Backing node\*\*\s*:\s*`(?:[^`]*/)?(?P<name>node_[a-z0-9_]+)/?`",
        re.IGNORECASE,
    ),
    # Short body form: **Backing node**: `node_foo`
    re.compile(
        r"\*\*Backing node\*\*\s*:\s*`(?P<name>node_[a-z0-9_]+)`",
        re.IGNORECASE,
    ),
    # Inline heading form: ... · **Backing node**: `.../node_foo/` · ...
    re.compile(
        r"Backing\s+node\*\*\s*:\s*`(?:[^`]*/)?(?P<name>node_[a-z0-9_]+)/?`",
        re.IGNORECASE,
    ),
    # Plain body form without bold: Backing node: `node_foo`
    re.compile(
        r"(?<!\*)Backing node\s*:\s*`(?:[^`]*/)?(?P<name>node_[a-z0-9_]+)/?`",
        re.IGNORECASE,
    ),
    # YAML frontmatter form: backing_node: "node_foo"
    re.compile(
        r"^backing_node\s*:\s*[\"']?(?P<name>node_[a-z0-9_]+)[\"']?",
        re.MULTILINE,
    ),
)

# Phase 3c — markers that indicate a SKILL.md describes STATEFUL orchestration.
# A skill with no backing node is only a FACADE if it describes stateful
# orchestration. Pure-instruction / interactive skills (no state files, no wave
# caps, no in-flight tracking) are exempt.
_STATEFUL_ORCHESTRATION_MARKERS: tuple[str, ...] = (
    "dispatched.yaml",
    "state.yaml",
    "wave cap",
    "wave-cap",
    "in-flight",
    "in flight",
    "phase tracking",
    "state file",
    "state tracking",
    "dispatch queue",
)

# Phase 3c — explicit pure-instruction opt-out tokens. When a SKILL.md declares
# itself instruction-only, it is never a FACADE regardless of length.
_PURE_INSTRUCTION_MARKERS: tuple[str, ...] = (
    "pure instruction",
    "pure-instruction",
    "instruction-only",
    "instruction only",
)

# FACADE length threshold from the SKILL.md heuristic (Phase 3c).
_FACADE_MIN_LINES = 200


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

    nodes_roots = _resolve_nodes_roots(request.nodes_root)
    return [_audit_skill(skill, nodes_roots=nodes_roots) for skill in skills]


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
                    node_name=_extract_backing_node(content),
                )
            )
    return skills


def _audit_skill(skill: _SkillShim, *, nodes_roots: list[Path]) -> ModelSkillVerdict:
    gaps: list[str] = []
    stubs_found: list[str] = []

    # Phase 3c — pure-instruction exemption. A skill with no DECLARED backing
    # node is only a FACADE if its SKILL.md describes stateful orchestration AND
    # it is not marked instruction-only. Otherwise it is intentionally a
    # pure-instruction / interactive skill and is WORKS, not a gap.
    if skill.node_name is None:
        if _is_facade_without_backing(skill.content):
            gaps.append(
                f"{skill.path}: describes stateful orchestration but declares "
                "no backing node (FACADE)"
            )
            status = "gap"
        else:
            status = "ok"
        return ModelSkillVerdict(
            name=skill.name,
            status=status,
            stubs_found=stubs_found,
            gaps=gaps,
        )

    node_dir = _resolve_node_dir(skill.node_name, nodes_roots)
    if node_dir is None:
        searched = ", ".join(str(root / skill.node_name) for root in nodes_roots)
        gaps.append(
            f"{skill.node_name}: backing node not found under any nodes root "
            f"({searched})"
        )
        return ModelSkillVerdict(
            name=skill.name,
            status="gap",
            stubs_found=stubs_found,
            gaps=gaps,
        )

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

    handler_paths = _handler_paths(contract, node_dir=node_dir)
    if handler_paths:
        missing = [str(p) for p in handler_paths if not p.is_file()]
        if missing:
            gaps.append(
                f"{skill.node_name}: handler file(s) not found: {', '.join(missing)}"
            )
        for handler_path in handler_paths:
            if not handler_path.is_file():
                continue
            handler_text = handler_path.read_text(encoding="utf-8")
            if any(marker in handler_text for marker in _STUB_MARKERS):
                stubs_found.append(str(handler_path))
    elif not _node_is_wired(contract, node_dir=node_dir):
        # No resolvable handler files AND no other wiring evidence (node.py
        # entrypoint, declared shared handler, or components handler block).
        # Only then is the wiring genuinely absent. Shell ORCHESTRATOR nodes
        # (e.g. omniclaude node_skill_*_orchestrator) dispatch to a shared
        # handler via node.py and have no local handlers/ dir — they are wired.
        gaps.append(
            f"{skill.node_name}: no handler.module, handler_routing, "
            "handlers/ directory, node entrypoint, or declared handler"
        )

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


def _is_facade_without_backing(content: str) -> bool:
    """Return True when a no-backing-node skill is a FACADE per Phase 3c.

    A skill with no declared backing node is a FACADE only when its SKILL.md
    describes stateful orchestration (state files, wave caps, in-flight
    tracking) AND does not declare itself pure-instruction. Pure-instruction /
    interactive skills are exempt.
    """
    lowered = content.lower()
    if any(marker in lowered for marker in _PURE_INSTRUCTION_MARKERS):
        return False
    line_count = content.count("\n") + 1
    if line_count < _FACADE_MIN_LINES:
        return False
    return any(marker in lowered for marker in _STATEFUL_ORCHESTRATION_MARKERS)


def _resolve_skill_roots(raw_roots: list[str] | None) -> list[Path]:
    if raw_roots:
        return [Path(root) for root in raw_roots]
    return [
        _REPO_ROOT / "plugins" / "onex" / "skills",
        _REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "skills",
    ]


def _resolve_nodes_roots(raw_root: str | None) -> list[Path]:
    """Return candidate nodes roots for backing-node resolution.

    Backing nodes may live in the omnimarket nodes root OR an omniclaude
    shell-node root (node_skill_*_orchestrator). An explicit ``nodes_root``
    request field is honored first (and kept first so test fixtures resolve
    against their own tmp tree before any sibling layout).
    """
    roots: list[Path] = []
    if raw_root:
        roots.append(Path(raw_root))
    roots.append(_REPO_ROOT / "src" / "omnimarket" / "nodes")
    # Sibling omniclaude shell-node root: <omni_home>/omniclaude/src/...
    omni_home = _REPO_ROOT.parent
    roots.append(omni_home / "omniclaude" / "src" / "omniclaude" / "nodes")
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def _resolve_node_dir(node_name: str, nodes_roots: list[Path]) -> Path | None:
    for root in nodes_roots:
        candidate = root / node_name
        if candidate.is_dir():
            return candidate
    return None


def _skill_name(path: Path, content: str) -> str:
    frontmatter = _frontmatter(content)
    name = frontmatter.get("name")
    return str(name) if name else path.parent.name


def _extract_backing_node(content: str) -> str | None:
    """Return the DECLARED backing node name, or None.

    Only an explicit "Backing node" field or ``backing_node:`` frontmatter key
    is honored. Prose ``node_*`` tokens (``<node_path>``, ``node_name``,
    ``node_type``) are intentionally ignored — that prose-grab was the source of
    the OMN-13512 false positives.
    """
    for pattern in _BACKING_NODE_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group("name")
    return None


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


def _handler_paths(contract: dict[str, Any], *, node_dir: Path) -> list[Path]:
    """Return handler file paths declared by the contract or present on disk.

    Resolution order:
    1. Top-level ``handler.module`` (COMPUTE / EFFECT / REDUCER nodes).
    2. ``handler_routing`` map (canonical ORCHESTRATOR nodes route to sub-node
       handlers via this map and have no top-level ``handler.module``).
    3. Fallback to the node's own ``handlers/handler_*.py`` files on disk.

    An empty list means no handler wiring could be located at all.
    """
    paths: list[Path] = []

    module = _module_value(contract.get("handler"))
    if module is not None:
        resolved = _module_to_path(module, node_dir=node_dir)
        if resolved is not None:
            paths.append(resolved)

    routing = contract.get("handler_routing")
    if isinstance(routing, dict):
        for route in routing.values():
            route_module = _module_value(route)
            if route_module is None and isinstance(route, str):
                route_module = route if route.startswith("omnimarket.") else None
            if route_module is not None:
                resolved = _module_to_path(route_module, node_dir=node_dir)
                if resolved is not None:
                    paths.append(resolved)

    if not paths:
        handlers_dir = node_dir / "handlers"
        if handlers_dir.is_dir():
            paths.extend(sorted(handlers_dir.glob("handler_*.py")))

    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _node_is_wired(contract: dict[str, Any], *, node_dir: Path) -> bool:
    """Return True when the node has wiring evidence beyond handlers/ files.

    Shell ORCHESTRATOR nodes (omniclaude node_skill_*_orchestrator) dispatch to
    a shared handler via a ``node.py`` entrypoint and declare the handler under
    a ``components``/``nodes`` block rather than a top-level ``handler.module``.
    Such nodes are wired even though they have no local ``handlers/`` directory.
    """
    # A node.py entrypoint is concrete evidence of a runnable node shell.
    if (node_dir / "node.py").is_file():
        return True

    # A declared handler component anywhere in the contract counts as wiring.
    components = contract.get("components")
    if isinstance(components, list):
        for component in components:
            if isinstance(component, dict) and component.get("type") == "handler":
                return True

    # A declared dispatch handler method (handler_method) counts as wiring.
    if _contract_declares_handler_method(contract):
        return True

    return False


def _contract_declares_handler_method(node: Any, _depth: int = 0) -> bool:
    """Return True when a ``handler_method`` key appears anywhere in the contract."""
    if _depth > 6:
        return False
    if isinstance(node, dict):
        if isinstance(node.get("handler_method"), str):
            return True
        return any(
            _contract_declares_handler_method(v, _depth + 1) for v in node.values()
        )
    if isinstance(node, list):
        return any(_contract_declares_handler_method(v, _depth + 1) for v in node)
    return False


def _module_value(node: Any) -> str | None:
    """Extract a dotted handler module string from a contract sub-mapping."""
    if isinstance(node, dict):
        module = node.get("module")
        if isinstance(module, str):
            return module
        handler = node.get("handler")
        if isinstance(handler, str):
            return handler
    return None


def _module_to_path(module: str, *, node_dir: Path) -> Path | None:
    """Resolve a dotted handler module to a file path.

    Handles both omnimarket node modules and, when the dotted module does not
    map under the known package prefixes, falls back to the node's own
    ``handlers/`` directory by leaf module name so cross-layer shell nodes still
    resolve.
    """
    prefix = "omnimarket.nodes."
    if module.startswith(prefix):
        relative = module.removeprefix(prefix)
        return node_dir.parents[0] / Path(*relative.split(".")).with_suffix(".py")
    # Generic dotted module: place the leaf file under the node's handlers/ dir.
    leaf = module.split(".")[-1]
    return node_dir / "handlers" / f"{leaf}.py"
