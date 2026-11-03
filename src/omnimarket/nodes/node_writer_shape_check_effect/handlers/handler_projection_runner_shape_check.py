# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: refuse the two silent-failure shapes of a projection writer (OMN-18883).

The projection wiring path calls a routed entry with the bare event dict plus
injected ``_db`` and envelope keys, and expects the entry to WRITE. Two shapes
look right and write nothing, with consumer lag and topic watermarks reading
healthy (OMN-18769 C2):

1. **Typed fold on the writer path.** A node that declares ``db_io.db_tables``
   routes a class whose ``handle`` takes a typed def-B request instead of the
   injected dict, and that is either not a ``BaseProjectionRunner`` subclass or is
   one dispatched in process. The runtime hands it the dict, it validates and
   returns, and no row is written. Route the thin writer subclass, which calls the
   fold in process.
   The scope is every ``node_projection_*`` node whose contract declares
   ``db_io.db_tables`` and is not an EFFECT node (an effect node is triggered by
   a tick, not by the projection event path).
2. **Runner with no dispatch owner.** A ``BaseProjectionRunner`` subclass with
   neither ``onex_runtime_inprocess_dispatch = True`` nor an entry in the frozen
   ``dedicated_deployments`` set. The shared runtime treats a runner-shaped class
   without the declaration as standalone and skips it silently, so the class
   consumes nothing unless a dedicated writer Deployment owns its topics.

It also holds the frozen, shrink-only set of every ``BaseProjectionRunner``
subclass still standing: a subclass outside the set is growth and is refused, and
a set entry whose class is gone is stale and is refused, so the set only shrinks.

The Deployment manifests live in another repository, so ``dedicated_deployments``
is spelled in this repository, in ``projection_runner_frozen_set.py``.
"""

import ast
from pathlib import Path
from typing import Final

import yaml

from omnimarket.nodes.node_writer_shape_check_effect.models import (
    ModelProjectionRunnerShapeFinding,
    ModelProjectionRunnerShapeRequest,
    ModelProjectionRunnerShapeResult,
)

RULE_TYPED_FOLD: Final[str] = "typed-fold-on-writer-path"
RULE_UNDECLARED: Final[str] = "undeclared-dispatch"
RULE_GROWTH: Final[str] = "runner-baseline-growth"
RULE_STALE: Final[str] = "runner-baseline-stale"
RULE_VACUOUS: Final[str] = "scan-vacuous"

_RUNNER_BASE: Final[str] = "BaseProjectionRunner"
_INPROCESS_ATTR: Final[str] = "onex_runtime_inprocess_dispatch"
_PROJECTION_PREFIX: Final[str] = "node_projection_"
_MAPPING_LIKE: Final[frozenset[str]] = frozenset(
    {"dict", "Mapping", "MutableMapping", "Any", "object"}
)


def _base_names(cls: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _declares_inprocess(cls: ast.ClassDef) -> bool:
    for stmt in cls.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id == _INPROCESS_ATTR
                and isinstance(value, ast.Constant)
                and value.value is True
            ):
                return True
    return False


def _is_mapping_like(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.BinOp):
        return _is_mapping_like(annotation.left) and _is_mapping_like(annotation.right)
    if isinstance(annotation, ast.Subscript):
        return _is_mapping_like(annotation.value)
    if isinstance(annotation, ast.Name):
        return annotation.id in _MAPPING_LIKE
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in _MAPPING_LIKE
    return isinstance(annotation, ast.Constant) and annotation.value is None


def _handle_is_model_typed(cls: ast.ClassDef) -> bool:
    """Whether ``handle`` annotates its request with a model type (not a mapping)."""
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            stmt.name == "handle"
        ):
            params = [a for a in stmt.args.args if a.arg not in ("self", "cls")]
            if not params or params[0].annotation is None:
                return False
            return not _is_mapping_like(params[0].annotation)
    return False


def _module_path(package_root: Path, dotted: str) -> Path:
    return package_root.joinpath(*dotted.split(".")).with_suffix(".py")


def _routed_entries(contract: dict[str, object]) -> list[tuple[str, str]]:
    routing = contract.get("handler_routing")
    if not isinstance(routing, dict):
        return []
    entries: list[tuple[str, str]] = []
    for item in routing.get("handlers") or []:
        handler = item.get("handler") if isinstance(item, dict) else None
        if isinstance(handler, dict) and handler.get("name") and handler.get("module"):
            entries.append((str(handler["name"]), str(handler["module"])))
    return entries


def _declares_db_tables(contract: dict[str, object]) -> bool:
    db_io = contract.get("db_io")
    return isinstance(db_io, dict) and bool(db_io.get("db_tables"))


def _classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}


class HandlerProjectionRunnerShapeCheck:
    def handle(
        self, request: ModelProjectionRunnerShapeRequest
    ) -> ModelProjectionRunnerShapeResult:
        nodes_root = Path(request.nodes_root)
        package_root = Path(request.package_root)
        findings: list[ModelProjectionRunnerShapeFinding] = []
        runners: dict[str, bool] = {}  # "module:Class" -> declares in-process
        trees: dict[str, ast.Module] = {}

        for py in sorted(nodes_root.glob("node_*/**/*.py")):
            if "tests" in py.parts or py.name.startswith("test_"):
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            module = ".".join(py.relative_to(package_root).with_suffix("").parts)
            trees[module] = tree
            for name, cls in _classes(tree).items():
                if _RUNNER_BASE in _base_names(cls):
                    runners[f"{module}:{name}"] = _declares_inprocess(cls)

        for node_dir in sorted(p for p in nodes_root.glob("node_*") if p.is_dir()):
            findings.extend(self._typed_fold_findings(node_dir, package_root, trees))

        findings.extend(self._dispatch_findings(runners, request))
        if len(runners) < request.min_runners:
            findings.append(
                ModelProjectionRunnerShapeFinding(
                    rule=RULE_VACUOUS,
                    subject=str(nodes_root),
                    message=(
                        f"saw {len(runners)} runner subclasses, below the floor of "
                        f"{request.min_runners}; the scan is broken, not the tree clean"
                    ),
                )
            )
        return ModelProjectionRunnerShapeResult(
            runners_seen=len(runners), findings=tuple(findings)
        )

    def _typed_fold_findings(
        self, node_dir: Path, package_root: Path, trees: dict[str, ast.Module]
    ) -> list[ModelProjectionRunnerShapeFinding]:
        contract_path = node_dir / "contract.yaml"
        if not node_dir.name.startswith(_PROJECTION_PREFIX) or not (
            contract_path.is_file()
        ):
            return []
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        if not isinstance(contract, dict) or not _declares_db_tables(contract):
            return []
        if str(contract.get("node_type", "")).lower().startswith("effect"):
            return []
        found: list[ModelProjectionRunnerShapeFinding] = []
        for name, module in _routed_entries(contract):
            tree = trees.get(module)
            if tree is None:
                path = _module_path(package_root, module)
                if not path.is_file():
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
            cls = _classes(tree).get(name)
            if cls is None:
                continue
            # A runner subclass is dispatched in process only when it declares it;
            # an undeclared one is standalone and its handle is never the entry.
            is_runner = _RUNNER_BASE in _base_names(cls)
            if is_runner and not _declares_inprocess(cls):
                continue
            if _handle_is_model_typed(cls):
                found.append(
                    ModelProjectionRunnerShapeFinding(
                        rule=RULE_TYPED_FOLD,
                        subject=f"{module}:{name}",
                        message=(
                            "owns topics of a contract that declares db_io.db_tables "
                            "and its handle takes a typed request, but it is not a "
                            "standalone runner; the projection wiring path hands it "
                            "the injected dict, so it validates and returns without "
                            "writing. Route a thin writer subclass of "
                            "BaseProjectionRunner that calls this fold in process"
                        ),
                    )
                )
        return found

    def _dispatch_findings(
        self, runners: dict[str, bool], request: ModelProjectionRunnerShapeRequest
    ) -> list[ModelProjectionRunnerShapeFinding]:
        found: list[ModelProjectionRunnerShapeFinding] = []
        frozen = set(request.frozen_runners)
        dedicated = set(request.dedicated_deployments)
        for key, inprocess in sorted(runners.items()):
            if not inprocess and key not in dedicated:
                found.append(
                    ModelProjectionRunnerShapeFinding(
                        rule=RULE_UNDECLARED,
                        subject=key,
                        message=(
                            f"declares neither {_INPROCESS_ATTR} = True nor a dedicated "
                            "writer Deployment owning its topics; the shared runtime "
                            "treats a runner-shaped class without the declaration as "
                            "standalone and skips it silently"
                        ),
                    )
                )
            if key not in frozen:
                found.append(
                    ModelProjectionRunnerShapeFinding(
                        rule=RULE_GROWTH,
                        subject=key,
                        message=(
                            "is a BaseProjectionRunner subclass outside the frozen "
                            "set; the set only shrinks, so write the projection as a "
                            "pure fold plus the generic write effect instead"
                        ),
                    )
                )
        for key in sorted(frozen - runners.keys()):
            found.append(
                ModelProjectionRunnerShapeFinding(
                    rule=RULE_STALE,
                    subject=key,
                    message="is in the frozen set but no such subclass exists; delete the entry",
                )
            )
        for key in sorted(dedicated - runners.keys()):
            found.append(
                ModelProjectionRunnerShapeFinding(
                    rule=RULE_STALE,
                    subject=key,
                    message="is in the dedicated-deployment set but no such subclass exists; delete the entry",
                )
            )
        return found
