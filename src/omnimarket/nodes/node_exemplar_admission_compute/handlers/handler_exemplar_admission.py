# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure admission decision of TC.5 (OMN-21128).

Reading the repository at the commit, classifying its bindings, and reading check-runs
are the caller's job. This handler parses supplied text with ast, reads no files,
uses no network or clock, and runs no subprocess.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from typing import get_args

from omnimarket.nodes.node_exemplar_admission_compute.models.model_exemplar_admission import (
    ModelAdmissionDecision,
    ModelAdmissionViolation,
    ModelExemplarAdmissionRequest,
    ModelExemplarAdmissionResult,
    ModelExemplarAdmissionRules,
    ModelExemplarCandidate,
    ModelGateReceipt,
    ViolationCode,
)

_VIOLATION_ORDER = get_args(ViolationCode)
_UNTYPED_PREFIXES = ("dict[", "Dict[", "typing.Dict[", "list[", "Mapping[")
type Handle = ast.FunctionDef | ast.AsyncFunctionDef


def _violation(code: ViolationCode, detail: str = "") -> ModelAdmissionViolation:
    return ModelAdmissionViolation(code=code, detail=detail)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _matches(module: str, entries: tuple[str, ...]) -> bool:
    return any(module == entry or module.startswith(entry + ".") for entry in entries)


def _imports(tree: ast.AST) -> Iterator[ast.Import | ast.ImportFrom]:
    """Runtime imports, including nested scopes, excluding TYPE_CHECKING bodies."""
    if isinstance(tree, (ast.Import, ast.ImportFrom)):
        yield tree
    elif isinstance(tree, ast.If) and _dotted(tree.test) in (
        "TYPE_CHECKING",
        "typing.TYPE_CHECKING",
    ):
        for statement in tree.orelse:
            yield from _imports(statement)
    else:
        for child in ast.iter_child_nodes(tree):
            yield from _imports(child)


def _import_modules(
    node: ast.Import | ast.ImportFrom, module: str, candidate: ModelExemplarCandidate
) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        is_package = module == candidate.node_package or any(
            name.startswith(module + ".") for name in candidate.sources
        )
        package = module if is_package else module.rpartition(".")[0]
        parts = package.split(".")
        if node.level > len(parts):
            return []
        base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
    return [base] if base else []


def _local_imports(
    node: ast.Import | ast.ImportFrom, base: str, candidate: ModelExemplarCandidate
) -> list[str]:
    """Prefer supplied submodules in from-imports; otherwise require the base."""
    if isinstance(node, ast.Import):
        return [base]
    children = [
        f"{base}.{alias.name}"
        for alias in node.names
        if f"{base}.{alias.name}" in candidate.sources
    ]
    if base in candidate.sources or not children:
        return [base, *children]
    return children


def _find_handle(
    candidate: ModelExemplarCandidate, trees: dict[str, ast.Module]
) -> Handle | None:
    classes: dict[str, ast.ClassDef] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)
    root = next(
        (
            node
            for node in ast.walk(trees[candidate.handler_module])
            if isinstance(node, ast.ClassDef) and node.name == candidate.handler_class
        ),
        None,
    )
    visited: set[int] = set()

    def visit(cls: ast.ClassDef) -> Handle | None:
        if id(cls) in visited:
            return None
        visited.add(id(cls))
        for item in cls.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "handle"
            ):
                return item
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                found = visit(classes[base.id])
                if found is not None:
                    return found
        return None

    return visit(root) if root is not None else None


def _untyped(annotation: ast.expr | None, rules: ModelExemplarAdmissionRules) -> bool:
    if annotation is None:
        return True
    text = ast.unparse(annotation)
    return text in rules.untyped_annotations or text.startswith(_UNTYPED_PREFIXES)


def _shape(
    handle: Handle, rules: ModelExemplarAdmissionRules
) -> list[ModelAdmissionViolation]:
    violations: list[ModelAdmissionViolation] = []
    args = handle.args
    positional = [*args.posonlyargs, *args.args]
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    if args.kwarg is not None:
        violations.append(_violation("handle_kwargs"))
    if args.vararg is not None:
        violations.append(_violation("handle_varargs"))
    if not positional and args.vararg is None:
        violations.append(_violation("handle_zeroarg"))
    if len(positional) > 1:
        violations.append(_violation("handle_multi_positional"))
    if any(default is None for default in args.kw_defaults):
        violations.append(_violation("handle_keyword_only"))
    if len(positional) == 1 and _untyped(positional[0].annotation, rules):
        violations.append(_violation("handle_param_untyped"))
    if _untyped(handle.returns, rules):
        violations.append(_violation("handle_return_untyped"))
    return violations


def _purity(
    candidate: ModelExemplarCandidate,
    trees: dict[str, ast.Module],
    rules: ModelExemplarAdmissionRules,
) -> list[ModelAdmissionViolation]:
    violations: list[ModelAdmissionViolation] = []
    visited: set[str] = set()

    def scan(module: str) -> None:
        if module in visited:
            return
        visited.add(module)
        if module not in candidate.sources:
            violations.append(_violation("import_source_missing", module))
            return
        tree = trees.get(module)
        if tree is None:
            return
        for node in _imports(tree):
            for imported in _import_modules(node, module, candidate):
                for kind, entries in rules.impure_import_modules.items():
                    if _matches(imported, entries):
                        violations.append(
                            _violation(
                                "impure_import", f"{kind}:{imported} in {module}"
                            )
                        )
                if imported == candidate.node_package or imported.startswith(
                    candidate.node_package + "."
                ):
                    for local in _local_imports(node, imported, candidate):
                        scan(local)
        for expression in ast.walk(tree):
            if (
                isinstance(expression, ast.Call)
                and _dotted(expression.func) in rules.clock_calls
            ):
                violations.append(
                    _violation(
                        "impure_import", f"clock:{_dotted(expression.func)} in {module}"
                    )
                )

    scan(candidate.handler_module)
    return violations


def _deny(
    candidate: ModelExemplarCandidate,
    trees: dict[str, ast.Module],
    rules: ModelExemplarAdmissionRules,
) -> list[ModelAdmissionViolation]:
    violations: list[ModelAdmissionViolation] = []
    pattern = re.compile(rules.deny_secret_name_pattern)

    def secret(name: str, value: ast.expr | None, module: str) -> None:
        if (
            pattern.search(name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value
        ):
            violations.append(_violation("deny_secret_literal", f"{name} in {module}"))

    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        secret(target.id, node.value, module)
                    elif isinstance(target, ast.Attribute):
                        secret(target.attr, node.value, module)
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg is not None:
                        secret(keyword.arg, keyword.value, module)
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in rules.deny_call_names
                ):
                    violations.append(
                        _violation("deny_eval_exec", f"{node.func.id} in {module}")
                    )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for imported in _import_modules(node, module, candidate):
                    if _matches(imported, rules.deny_import_modules):
                        violations.append(
                            _violation("deny_pickle", f"{imported} in {module}")
                        )
    return violations


def _ast_violations(
    candidate: ModelExemplarCandidate, rules: ModelExemplarAdmissionRules
) -> list[ModelAdmissionViolation]:
    if candidate.handler_module not in candidate.sources:
        return [_violation("handler_source_missing")]
    trees: dict[str, ast.Module] = {}
    violations: list[ModelAdmissionViolation] = []
    for module, source in candidate.sources.items():
        try:
            trees[module] = ast.parse(source)
        except (SyntaxError, ValueError, TypeError, RecursionError):
            violations.append(_violation("handler_unparseable", module))
    if candidate.handler_module in trees:
        handle = _find_handle(candidate, trees)
        violations.extend(
            [_violation("handle_not_found")]
            if handle is None
            else _shape(handle, rules)
        )
    if candidate.node_archetype == "compute":
        violations.extend(_purity(candidate, trees, rules))
    violations.extend(_deny(candidate, trees, rules))
    return violations


def _receipt(
    candidate: ModelExemplarCandidate, commit: str
) -> tuple[list[ModelAdmissionViolation], list[int]]:
    receipt = candidate.test_receipt
    if receipt is None:
        return [_violation("test_receipt_missing")], []
    violations: list[ModelAdmissionViolation] = []
    chosen_ids: list[int] = []
    if not receipt.required_check_names:
        violations.append(_violation("receipt_no_required_checks"))
    for name in sorted(receipt.required_check_names):
        runs = [run for run in receipt.check_runs if run.name == name]
        matching = [run for run in runs if run.head_sha == commit]
        if not runs:
            violations.append(_violation("required_check_missing", name))
        elif not matching:
            violations.append(_violation("receipt_run_other_commit", name))
        else:
            chosen = max(matching, key=lambda run: run.id)
            chosen_ids.append(chosen.id)
            if chosen.status != "completed" or chosen.conclusion != "success":
                violations.append(
                    _violation(
                        "required_check_not_success",
                        f"{name}: {chosen.status}/{chosen.conclusion} run {chosen.id}",
                    )
                )
    if not any(
        path == receipt.node_test_path
        or path.startswith(receipt.node_test_path.rstrip("/") + "/")
        for path in receipt.collected_test_paths
    ):
        violations.append(_violation("node_tests_not_collected"))
    return violations, sorted(chosen_ids)


def _decide(
    request: ModelExemplarAdmissionRequest, candidate: ModelExemplarCandidate
) -> ModelAdmissionDecision:
    violations: list[ModelAdmissionViolation] = []
    if (
        request.repository_private
        or request.repository in request.rules.gate_absent_repositories
    ):
        violations.append(_violation("gate_absent_in_repository"))
    if candidate.node_id in request.baseline_node_ids:
        violations.append(_violation("in_baseline"))
    if not candidate.bindings:
        violations.append(_violation("no_bindings"))
    elif not candidate.bindings[0].is_canonical:
        first = candidate.bindings[0]
        violations.append(
            _violation("gate_failed", f"{first.category}: {first.detail or ''}")
        )
    for index, binding in enumerate(candidate.bindings[1:], start=1):
        if not binding.is_canonical:
            violations.append(
                _violation(
                    "binding_non_canonical",
                    f"binding[{index}] {binding.handler_module}: {binding.category}",
                )
            )
    violations.extend(_ast_violations(candidate, request.rules))
    # Stable ordering keeps repeated violations in source/binding order. Receipt checks
    # below retain their required-name order rather than grouping by failure code.
    violations.sort(key=lambda violation: _VIOLATION_ORDER.index(violation.code))
    if not candidate.contract_valid:
        violations.append(_violation("contract_invalid"))
    if not candidate.unit_test_dir_present:
        violations.append(_violation("unit_test_dir_missing"))
    receipt_violations, check_ids = _receipt(candidate, request.commit)
    violations.extend(receipt_violations)
    return ModelAdmissionDecision(
        node_id=candidate.node_id,
        node_archetype=candidate.node_archetype,
        contract_path=candidate.contract_path,
        handler_module=candidate.handler_module,
        admitted=not violations,
        violations=violations,
        coverage_state="unknown" if candidate.coverage_percent is None else "known",
        coverage_percent=candidate.coverage_percent,
        handler_lines=len(
            candidate.sources.get(candidate.handler_module, "").splitlines()
        ),
        rank=None,
        receipt=None
        if violations
        else ModelGateReceipt(
            gate=request.gate,
            gate_version=request.gate_version,
            baseline_sha256=request.baseline_sha256,
            result="pass",
            commit=request.commit,
            check_run_ids=check_ids,
        ),
    )


class HandlerExemplarAdmission:
    """Decide every candidate and rank admitted nodes within their archetypes."""

    def handle(
        self, request: ModelExemplarAdmissionRequest
    ) -> ModelExemplarAdmissionResult:
        decisions = [_decide(request, candidate) for candidate in request.candidates]
        groups: dict[str, list[int]] = {}
        for index, decision in enumerate(decisions):
            if decision.admitted:
                groups.setdefault(decision.node_archetype, []).append(index)
        admitted_by_archetype: dict[str, list[str]] = {}
        for archetype, indices in groups.items():
            indices.sort(
                key=lambda index: (
                    decisions[index].handler_lines,
                    decisions[index].contract_path,
                )
            )
            admitted_by_archetype[archetype] = []
            for rank, index in enumerate(indices, start=1):
                decisions[index] = decisions[index].model_copy(update={"rank": rank})
                admitted_by_archetype[archetype].append(decisions[index].node_id)
        admitted_count = sum(decision.admitted for decision in decisions)
        return ModelExemplarAdmissionResult(
            repository=request.repository,
            commit=request.commit,
            gate=request.gate,
            gate_version=request.gate_version,
            rules_version=request.rules.rules_version,
            pure_import_list_version=request.rules.pure_import_list_version,
            decisions=decisions,
            admitted_by_archetype=admitted_by_archetype,
            admitted_count=admitted_count,
            refused_count=len(decisions) - admitted_count,
        )
