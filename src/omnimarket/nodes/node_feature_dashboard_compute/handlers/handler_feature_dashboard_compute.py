# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFeatureDashboardCompute.

Pure skill connectivity audit across the native Onex skill/node layers. The
handler reads repository files only and returns a deterministic coverage map.
It does not create tickets or touch UI/dashboard code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    DEFAULT_CHECK_TYPES,
    ModelFeatureDashboardRequest,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_result import (
    ModelFeatureDashboardResult,
)

_BACKING_NODE_RE = re.compile(r"Backing node:\s*`?(?P<path>[^`\n]+)`?", re.IGNORECASE)
_COMMAND_RE = re.compile(r"Command name:\s*`?(?P<command>[^`\n]+)`?", re.IGNORECASE)


class HandlerFeatureDashboardCompute:
    """Compute skill-to-node coverage and gaps from local repository files."""

    def handle(
        self, request: ModelFeatureDashboardRequest
    ) -> ModelFeatureDashboardResult:
        repo_root = _repo_root(request)
        checks = request.check_types or list(DEFAULT_CHECK_TYPES)
        skill_docs = _discover_skill_docs(repo_root, request.skills)

        coverage_report: dict[str, object] = {}
        gaps: list[dict[str, object]] = []
        for skill_name, skill_path in skill_docs.items():
            coverage = _coverage_for_skill(repo_root, skill_name, skill_path, checks)
            coverage_report[skill_name] = coverage
            for check_name, passed in coverage["checks"].items():
                if not passed:
                    gaps.append(
                        {
                            "skill": skill_name,
                            "check_type": check_name,
                            "severity": _severity(check_name),
                            "detail": coverage["details"].get(check_name, ""),
                        }
                    )

        status = "empty"
        if coverage_report:
            status = "complete" if not gaps else "partial"
        return ModelFeatureDashboardResult(
            coverage_report=coverage_report,
            gaps=gaps,
            status=status,
            skills_audited=len(coverage_report),
            checks_run=checks,
        )


def _repo_root(request: ModelFeatureDashboardRequest) -> Path:
    if request.repo_root:
        return Path(request.repo_root).expanduser().resolve()
    return Path(__file__).resolve().parents[5]


def _discover_skill_docs(
    repo_root: Path, requested_skills: list[str] | None
) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    roots = [
        repo_root / "plugins" / "onex" / "skills",
        repo_root / "src" / "omnimarket" / "adapters" / "codex" / "skills",
    ]
    requested = set(requested_skills or [])
    for root in roots:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*/SKILL.md")):
            name = skill_file.parent.name
            if requested and name not in requested:
                continue
            candidates.setdefault(name, skill_file)
    for skill_name in sorted(requested):
        candidates.setdefault(skill_name, Path(""))
    return dict(sorted(candidates.items()))


def _coverage_for_skill(
    repo_root: Path,
    skill_name: str,
    skill_path: Path,
    checks: list[str],
) -> dict[str, Any]:
    text = (
        skill_path.read_text(encoding="utf-8", errors="replace")
        if skill_path.is_file()
        else ""
    )
    node_path = _backing_node_path(repo_root, text, skill_name)
    contract = _load_contract(node_path / "contract.yaml") if node_path else {}
    handler = contract.get("handler") if isinstance(contract, dict) else {}
    event_bus = contract.get("event_bus") if isinstance(contract, dict) else {}
    command = _command_name(text)
    check_results: dict[str, bool] = {}
    details: dict[str, str] = {}

    def set_check(name: str, passed: bool, detail: str) -> None:
        if name in checks:
            check_results[name] = passed
            details[name] = detail

    set_check("skill_doc", skill_path.is_file(), str(skill_path or "missing SKILL.md"))
    set_check(
        "backing_node",
        node_path is not None and node_path.is_dir(),
        str(node_path or ""),
    )
    set_check(
        "contract",
        bool(contract) and contract.get("node_not_implemented") is not True,
        str(node_path / "contract.yaml") if node_path else "missing backing node",
    )
    handler_module = str(handler.get("module", "")) if isinstance(handler, dict) else ""
    set_check(
        "handler",
        bool(handler_module) and _module_to_path(repo_root, handler_module).is_file(),
        handler_module or "missing handler module",
    )
    set_check(
        "models",
        bool(node_path)
        and (node_path / "models").is_dir()
        and any((node_path / "models").glob("*.py")),
        str(node_path / "models") if node_path else "missing backing node",
    )
    set_check(
        "tests",
        bool(node_path)
        and (
            (node_path / "tests").is_dir()
            or (
                repo_root / "tests" / f"test_golden_chain_{_node_slug(node_path)}.py"
            ).is_file()
        ),
        str(node_path / "tests") if node_path else "missing backing node",
    )
    set_check(
        "entry_point",
        _pyproject_declares_entry(repo_root, node_path.name if node_path else ""),
        node_path.name if node_path else "missing backing node",
    )
    publish_topics = (
        event_bus.get("publish_topics", ()) if isinstance(event_bus, dict) else ()
    )
    subscribe_topics = (
        event_bus.get("subscribe_topics", ()) if isinstance(event_bus, dict) else ()
    )
    set_check(
        "runtime_topics",
        bool(command) and bool(publish_topics) and bool(subscribe_topics),
        command or "missing command name or topics",
    )
    return {
        "skill_path": str(skill_path),
        "node_path": str(node_path) if node_path else "",
        "command_name": command,
        "checks": check_results,
        "details": details,
        "coverage_score": round(
            sum(1 for passed in check_results.values() if passed)
            / max(len(check_results), 1),
            3,
        ),
    }


def _backing_node_path(
    repo_root: Path, skill_text: str, skill_name: str
) -> Path | None:
    match = _BACKING_NODE_RE.search(skill_text)
    if match:
        raw = match.group("path").strip()
        path = repo_root / raw
        if path.exists():
            return path
    node_name = "node_" + skill_name.replace("-", "_")
    for candidate in (repo_root / "src" / "omnimarket" / "nodes").glob(node_name + "*"):
        if candidate.is_dir():
            return candidate
    return None


def _command_name(skill_text: str) -> str:
    match = _COMMAND_RE.search(skill_text)
    return match.group("command").strip() if match else ""


def _load_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _module_to_path(repo_root: Path, module: str) -> Path:
    return repo_root / "src" / Path(*module.split(".")).with_suffix(".py")


def _pyproject_declares_entry(repo_root: Path, node_name: str) -> bool:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file() or not node_name:
        return False
    return node_name in pyproject.read_text(encoding="utf-8", errors="replace")


def _node_slug(node_path: Path | None) -> str:
    if node_path is None:
        return ""
    return node_path.name.removeprefix("node_")


def _severity(check_name: str) -> str:
    if check_name in {"backing_node", "contract", "handler"}:
        return "HIGH"
    if check_name in {"runtime_topics", "entry_point", "models"}:
        return "MEDIUM"
    return "LOW"
