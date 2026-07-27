# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFeatureDashboardCompute.

Pure skill connectivity audit across the native Onex skill/node layers.

Discovery is anchored on the LIVE dispatch registry — the
``skill_mapping.yaml`` that ``onex skill <name>`` itself loads from
``omnibase_infra.cli`` — instead of guessing at SKILL.md directory layouts.
A skill is "discovered" iff it is dispatch-registered; every registered
skill is then audited against the real surfaces its registry entry declares
(backing node package, contract.yaml, handler modules, models/result model,
tests, ``onex.nodes`` entry point, event-bus topics).

Two hard guarantees (both regression-pinned in tests):

* No vacuous pass — auditing zero skills raises instead of returning an
  empty success receipt.
* No silent false-0% — a dispatch-registered skill that scores 0 coverage
  produces a CRITICAL ``registry_inconsistency`` gap (a skill that is
  registered for dispatch cannot honestly be "0% connected"; a zero score
  means the audit's view of the topology is inconsistent, and that
  inconsistency must be loud).

The handler reads repository/package files only and returns a deterministic
coverage map. It does not create tickets or touch UI/dashboard code.
"""

from __future__ import annotations

import os
import re
from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    DEFAULT_CHECK_TYPES,
    ModelFeatureDashboardRequest,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_result import (
    ModelFeatureDashboardResult,
)

_ENTRY_POINT_GROUP = "onex.nodes"
_REGISTRY_PACKAGE = "omnibase_infra.cli"
_REGISTRY_FILENAME = "skill_mapping.yaml"
# Gap types that are not per-check failures.
GAP_REGISTRY_ENTRY = "registry_entry"
GAP_REGISTRY_INCONSISTENCY = "registry_inconsistency"


class HandlerFeatureDashboardCompute:
    """Compute skill-to-node coverage and gaps from the live skill registry."""

    def handle(
        self, request: ModelFeatureDashboardRequest
    ) -> ModelFeatureDashboardResult:
        repo_root = _repo_root(request)
        checks = request.check_types or list(DEFAULT_CHECK_TYPES)
        registry = _load_skill_registry(repo_root)
        audit_targets = _select_audit_targets(registry, request.skills)

        coverage_report: dict[str, object] = {}
        gaps: list[dict[str, object]] = []
        for skill_name in audit_targets:
            entry = registry.get(skill_name)
            if entry is None:
                coverage_report[skill_name] = _unregistered_row()
                gaps.append(
                    {
                        "skill": skill_name,
                        "check_type": GAP_REGISTRY_ENTRY,
                        "severity": "HIGH",
                        "detail": (
                            f"skill '{skill_name}' has no entry in the live "
                            f"dispatch registry ({_REGISTRY_FILENAME}); it is "
                            "not invocable via `onex skill`"
                        ),
                    }
                )
                continue
            coverage = _coverage_for_skill(repo_root, skill_name, entry, checks)
            coverage_report[skill_name] = coverage
            check_results: dict[str, bool] = coverage["checks"]
            for check_name, passed in check_results.items():
                if not passed:
                    gaps.append(
                        {
                            "skill": skill_name,
                            "check_type": check_name,
                            "severity": _severity(check_name),
                            "detail": coverage["details"].get(check_name, ""),
                        }
                    )
            # A dispatch-registered skill scoring 0 (or having nothing
            # evaluable) is a discovery inconsistency, never a quiet row.
            if not check_results or coverage["coverage_score"] == 0.0:
                gaps.append(
                    {
                        "skill": skill_name,
                        "check_type": GAP_REGISTRY_INCONSISTENCY,
                        "severity": "CRITICAL",
                        "detail": (
                            f"skill '{skill_name}' is dispatch-registered "
                            f"(backing node {entry['node_name']}) yet scored "
                            "0 coverage — the audit's topology view is "
                            "inconsistent with the live registry; this is a "
                            "discovery defect, not a real 0%-connected skill"
                        ),
                    }
                )

        if not coverage_report:
            raise RuntimeError(
                "feature_dashboard audited zero skills — refusing to emit a "
                "vacuous empty-success receipt (live registry loaded "
                f"{len(registry)} skills; filter={request.skills!r})"
            )

        status: Literal["complete", "partial"] = "complete" if not gaps else "partial"
        return ModelFeatureDashboardResult(
            coverage_report=coverage_report,
            gaps=gaps,
            status=status,
            skills_audited=len(coverage_report),
            checks_run=checks,
        )


def _repo_root(request: ModelFeatureDashboardRequest) -> Path | None:
    """Explicit source-tree root for file-level checks, if provided."""
    if request.repo_root:
        return Path(request.repo_root).expanduser().resolve()
    return None


def _source_checkout_root() -> Path | None:
    """Best-effort omnimarket source checkout root (None when installed)."""
    root = Path(__file__).resolve().parents[5]
    return root if (root / "pyproject.toml").is_file() else None


def _load_skill_registry(repo_root: Path | None) -> dict[str, dict[str, str]]:
    """Load the live `onex skill` dispatch registry (skill -> node mapping).

    Priority: a repo_root-local ``src/omnibase_infra/cli/skill_mapping.yaml``
    (test fixtures / omnibase_infra checkouts), else the installed
    ``omnibase_infra.cli`` package resource — the exact file the `onex skill`
    CLI resolves skills from.
    """
    raw_text = _registry_text(repo_root)
    parsed = yaml.safe_load(raw_text) or {}
    entries = parsed.get("skills") if isinstance(parsed, dict) else None
    registry: dict[str, dict[str, str]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        skill_name = str(entry.get("skill_name") or "").strip()
        node_name = str(entry.get("node_name") or "").strip()
        if not skill_name or not node_name:
            continue
        registry[skill_name] = {
            "node_name": node_name,
            "result_model": str(entry.get("result_model") or "").strip(),
        }
    if not registry:
        raise RuntimeError(
            f"live skill registry ({_REGISTRY_FILENAME}) resolved but "
            "contains zero skills — refusing vacuous audit"
        )
    return dict(sorted(registry.items()))


def _registry_text(repo_root: Path | None) -> str:
    if repo_root is not None:
        override = repo_root / "src" / "omnibase_infra" / "cli" / _REGISTRY_FILENAME
        if override.is_file():
            return override.read_text(encoding="utf-8")
    try:
        resource = importlib_resources.files(_REGISTRY_PACKAGE).joinpath(
            _REGISTRY_FILENAME
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"live skill registry unavailable: package {_REGISTRY_PACKAGE!r} "
            "is not installed and no repo_root override was found"
        ) from exc
    if not resource.is_file():
        raise RuntimeError(
            f"live skill registry unavailable: {_REGISTRY_PACKAGE} ships no "
            f"{_REGISTRY_FILENAME}"
        )
    return resource.read_text(encoding="utf-8")


def _select_audit_targets(
    registry: dict[str, dict[str, str]], requested: list[str] | None
) -> list[str]:
    if not requested:
        return list(registry)
    # Requested-but-unregistered skills stay in the audit so the missing
    # registry entry surfaces as a HIGH gap instead of silently vanishing.
    return sorted(set(requested))


def _unregistered_row() -> dict[str, Any]:
    return {
        "registered": False,
        "node_name": "",
        "node_path": "",
        "command_name": "",
        "checks": {},
        "details": {},
        "skipped_checks": {},
        "coverage_score": 0.0,
    }


def _coverage_for_skill(
    repo_root: Path | None,
    skill_name: str,
    entry: dict[str, str],
    checks: list[str],
) -> dict[str, Any]:
    node_name = entry["node_name"]
    node_dir = _node_dir(repo_root, node_name)
    contract = _load_contract(node_dir / "contract.yaml") if node_dir else {}

    check_results: dict[str, bool] = {}
    details: dict[str, str] = {}
    skipped: dict[str, str] = {}

    def set_check(name: str, passed: bool | None, detail: str) -> None:
        if name not in checks:
            return
        if passed is None:
            skipped[name] = detail
        else:
            check_results[name] = passed
            details[name] = detail

    doc_found, doc_detail = _skill_doc_check(repo_root, skill_name)
    set_check("skill_doc", doc_found, doc_detail)
    set_check(
        "backing_node",
        node_dir is not None,
        str(node_dir) if node_dir else f"node package {node_name} not resolvable",
    )
    set_check(
        "contract",
        bool(contract) and contract.get("node_not_implemented") is not True,
        str(node_dir / "contract.yaml") if node_dir else "missing backing node",
    )
    handler_modules = _declared_handler_modules(contract)
    set_check(
        "handler",
        bool(handler_modules)
        and all(_module_resolvable(repo_root, mod) for mod in handler_modules),
        ", ".join(handler_modules) or "no handler module declared in contract",
    )
    models_ok, models_detail = _models_check(
        repo_root, node_dir, entry.get("result_model", "")
    )
    set_check("models", models_ok, models_detail)
    tests_ok, tests_detail = _tests_check(repo_root, node_dir, node_name)
    set_check("tests", tests_ok, tests_detail)
    set_check(
        "entry_point",
        _entry_point_registered(repo_root, node_name),
        node_name,
    )
    event_bus = contract.get("event_bus") if isinstance(contract, dict) else None
    publish_topics = (
        event_bus.get("publish_topics") if isinstance(event_bus, dict) else None
    )
    subscribe_topics = (
        event_bus.get("subscribe_topics") if isinstance(event_bus, dict) else None
    )
    set_check(
        "runtime_topics",
        bool(publish_topics) and bool(subscribe_topics),
        "contract declares publish+subscribe topics"
        if publish_topics and subscribe_topics
        else "contract missing event_bus publish/subscribe topics",
    )
    return {
        "registered": True,
        "node_name": node_name,
        "node_path": str(node_dir) if node_dir else "",
        "command_name": f"onex skill {skill_name}",
        "checks": check_results,
        "details": details,
        "skipped_checks": skipped,
        "coverage_score": round(
            sum(1 for passed in check_results.values() if passed)
            / max(len(check_results), 1),
            3,
        ),
    }


def _node_dir(repo_root: Path | None, node_name: str) -> Path | None:
    if repo_root is not None:
        candidate = repo_root / "src" / "omnimarket" / "nodes" / node_name
        return candidate if candidate.is_dir() else None
    try:
        spec = find_spec(f"omnimarket.nodes.{node_name}")
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).parent


def _module_resolvable(repo_root: Path | None, module: str) -> bool:
    if not module:
        return False
    if repo_root is not None:
        path = (repo_root / "src" / Path(*module.split("."))).with_suffix(".py")
        return path.is_file()
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _declared_handler_modules(contract: dict[str, Any]) -> list[str]:
    """Handler modules a contract declares (top-level and handler_routing)."""
    modules: list[str] = []
    handler = contract.get("handler")
    if isinstance(handler, dict) and handler.get("module"):
        modules.append(str(handler["module"]))
    routing = contract.get("handler_routing")
    if isinstance(routing, dict):
        for item in routing.get("handlers") or []:
            if not isinstance(item, dict):
                continue
            routed = item.get("handler")
            if isinstance(routed, dict) and routed.get("module"):
                modules.append(str(routed["module"]))
    seen: set[str] = set()
    unique: list[str] = []
    for module in modules:
        if module not in seen:
            unique.append(module)
            seen.add(module)
    return unique


def _skill_doc_roots(repo_root: Path | None) -> list[Path]:
    bases: list[Path] = []
    if repo_root is not None:
        bases.append(repo_root)
    else:
        checkout = _source_checkout_root()
        if checkout is not None:
            bases.append(checkout)
    roots = [
        candidate
        for base in bases
        for candidate in (
            base / "plugins" / "onex" / "skills",
            base / "src" / "omnimarket" / "adapters" / "codex" / "skills",
        )
    ]
    # Opportunistic enrichment: the canonical SKILL.md surface for most
    # skills is omniclaude; include it when an OMNI_HOME workspace exists.
    omni_home = os.environ.get("OMNI_HOME", "")
    if omni_home:
        roots.append(Path(omni_home) / "omniclaude" / "plugins" / "onex" / "skills")
    return [root for root in roots if root.is_dir()]


def _skill_doc_check(
    repo_root: Path | None, skill_name: str
) -> tuple[bool | None, str]:
    roots = _skill_doc_roots(repo_root)
    if not roots:
        return None, "no SKILL.md roots available in this environment; skipped"
    candidates = {skill_name, skill_name.replace("_", "-")}
    for root in roots:
        for candidate in sorted(candidates):
            doc = root / candidate / "SKILL.md"
            if doc.is_file():
                return True, str(doc)
    return False, "SKILL.md not found under: " + ", ".join(str(r) for r in roots)


def _models_check(
    repo_root: Path | None, node_dir: Path | None, result_model: str
) -> tuple[bool, str]:
    if node_dir is not None:
        models_dir = node_dir / "models"
        if models_dir.is_dir() and any(models_dir.glob("*.py")):
            return True, str(models_dir)
    if result_model:
        module = result_model.rsplit(".", 1)[0]
        if _module_resolvable(repo_root, module):
            return True, f"typed result model {result_model}"
    return False, "no models/ package and no resolvable registry result_model"


def _tests_check(
    repo_root: Path | None, node_dir: Path | None, node_name: str
) -> tuple[bool | None, str]:
    if node_dir is not None:
        local_tests = node_dir / "tests"
        if local_tests.is_dir() and any(local_tests.glob("*.py")):
            return True, str(local_tests)
    root = repo_root if repo_root is not None else _source_checkout_root()
    if root is None or not (root / "tests").is_dir():
        return None, "no tests surface available in this environment; skipped"
    tests_root = root / "tests"
    node_test_dirs = [
        path for path in tests_root.glob(f"*/{node_name}") if path.is_dir()
    ]
    legacy = tests_root / f"test_golden_chain_{node_name.removeprefix('node_')}.py"
    if node_test_dirs or legacy.is_file():
        found = node_test_dirs[0] if node_test_dirs else legacy
        return True, str(found)
    return False, f"no test directory for {node_name} under {tests_root}"


def _entry_point_registered(repo_root: Path | None, node_name: str) -> bool:
    if repo_root is not None:
        pyproject = repo_root / "pyproject.toml"
        if not pyproject.is_file():
            return False
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        return (
            re.search(rf"^\s*{re.escape(node_name)}\s*=", text, re.MULTILINE)
            is not None
        )
    entry_points = importlib_metadata.entry_points(group=_ENTRY_POINT_GROUP)
    return any(entry_point.name == node_name for entry_point in entry_points)


def _load_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _severity(check_name: str) -> str:
    if check_name in {"backing_node", "contract", "handler"}:
        return "HIGH"
    if check_name in {"runtime_topics", "entry_point", "models"}:
        return "MEDIUM"
    return "LOW"
