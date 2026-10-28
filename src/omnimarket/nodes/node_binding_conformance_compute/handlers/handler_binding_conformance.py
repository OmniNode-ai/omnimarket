# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check supplied contracts and Python text against the caller's binding rules.

The table, concrete bindings, forbidden imports and private names are request data.
Pure: parses text without reading files, environment, time or network state.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping

import yaml

from omnimarket.models.binding_conformance.enum_binding_rule import EnumBindingRule
from omnimarket.models.binding_conformance.enum_binding_source_kind import (
    EnumBindingSourceKind,
)
from omnimarket.models.binding_conformance.model_binding_conformance_issue import (
    ModelBindingConformanceIssue,
)
from omnimarket.models.binding_conformance.model_binding_conformance_request import (
    ModelBindingConformanceRequest,
)
from omnimarket.models.binding_conformance.model_binding_conformance_result import (
    ModelBindingConformanceResult,
)
from omnimarket.models.binding_conformance.model_binding_protocol_table import (
    ModelBindingProtocolTable,
)
from omnimarket.models.binding_conformance.model_binding_source import (
    ModelBindingSource,
)
from omnimarket.models.binding_conformance.model_concrete_binding import (
    ModelConcreteBinding,
)
from omnimarket.models.binding_conformance.model_private_name import ModelPrivateName

_ENVIRONMENT_NAMES = frozenset({"environ", "getenv", "getenvb", "putenv"})
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S")
_ABSOLUTE_PATH = re.compile(r"^(~/|/)[^\s/][^\s]*$")


def _issue(
    source: ModelBindingSource,
    rule: EnumBindingRule,
    detail: str,
    line: int = 0,
) -> ModelBindingConformanceIssue:
    return ModelBindingConformanceIssue(
        node=source.node, path=source.path, line=line, rule=rule, detail=detail
    )


def _declared_protocols(text: str) -> set[str] | None:
    try:
        document: object = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(document, Mapping):
        return None
    dependencies: object = document.get("protocol_dependencies", [])
    if not isinstance(dependencies, list):
        return None
    declared: set[str] = set()
    for entry in dependencies:
        if not isinstance(entry, Mapping):
            return None
        protocol: object = entry.get("protocol")
        if not isinstance(protocol, str):
            return None
        declared.add(protocol)
    return declared


def _contract_issues(
    source: ModelBindingSource,
    required: tuple[str, ...],
    table: ModelBindingProtocolTable,
) -> list[ModelBindingConformanceIssue]:
    declared = _declared_protocols(source.text)
    if declared is None:
        return [
            _issue(
                source,
                EnumBindingRule.CONTRACT_UNREADABLE,
                "expected a mapping with a list of protocol dependency mappings",
            )
        ]
    codes = {entry.protocol: entry.code for entry in table.protocols}
    paths = {entry.code: entry.protocol for entry in table.protocols}
    issues = [
        _issue(
            source,
            EnumBindingRule.CONTRACT_UNKNOWN_PROTOCOL,
            f"unknown protocol {protocol}",
        )
        for protocol in sorted(declared - codes.keys())
    ]
    declared_codes = {codes[protocol] for protocol in declared if protocol in codes}
    issues.extend(
        _issue(
            source,
            EnumBindingRule.CONTRACT_MISSING_PROTOCOL,
            f"missing protocol {code}: {paths[code]}",
        )
        for code in sorted(set(required) - declared_codes)
    )
    issues.extend(
        _issue(
            source,
            EnumBindingRule.CONTRACT_EXTRA_PROTOCOL,
            f"extra protocol {code}",
        )
        for code in sorted(declared_codes - set(required))
    )
    return issues


def _import_modules(statement: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(statement, ast.Import):
        return tuple(alias.name for alias in statement.names)
    module = statement.module or ""
    return (
        module,
        *(
            f"{module}.{alias.name}" if module else alias.name
            for alias in statement.names
        ),
    )


def _matches_module(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _concrete_match(statement: ast.AST, binding: ModelConcreteBinding) -> str | None:
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        for module in _import_modules(statement):
            if _matches_module(module, binding.modules):
                return module
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                if alias.name in binding.classes:
                    return alias.name
    if isinstance(statement, ast.Call):
        function = statement.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else None
        )
        if name in binding.classes:
            return name
    return None


def _docstring_constants(tree: ast.Module) -> set[ast.Constant]:
    docstrings: set[ast.Constant] = set()
    for statement in ast.walk(tree):
        if (
            not isinstance(
                statement,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            or not statement.body
        ):
            continue
        first = statement.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(first.value)
    return docstrings


def _string_literals(tree: ast.Module) -> list[tuple[int, str]]:
    """Strings in use: docstrings are skipped and an f-string is read whole, holes as ``{}``."""
    docstrings = _docstring_constants(tree)
    fstring_parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    literals: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literals.append(
                (
                    node.lineno,
                    "".join(
                        part.value
                        if isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                        else "{}"
                        for part in node.values
                    ),
                )
            )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and id(node) not in fstring_parts
        ):
            literals.append((node.lineno, node.value))
    return literals


def _handler_issues(
    source: ModelBindingSource, tree: ast.Module
) -> list[ModelBindingConformanceIssue]:
    os_names = {
        alias.asname or "os"
        for statement in ast.walk(tree)
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == "os" or (alias.asname is None and alias.name.startswith("os."))
    }
    issues: list[ModelBindingConformanceIssue] = []
    for statement in ast.walk(tree):
        if (
            isinstance(statement, ast.Attribute)
            and isinstance(statement.value, ast.Name)
            and statement.value.id in os_names
            and statement.attr in _ENVIRONMENT_NAMES
        ):
            issues.append(
                _issue(
                    source,
                    EnumBindingRule.ENV_READ,
                    f"environment access through os.{statement.attr}",
                    statement.lineno,
                )
            )
        if isinstance(statement, ast.ImportFrom) and statement.module == "os":
            for alias in statement.names:
                if alias.name in _ENVIRONMENT_NAMES:
                    issues.append(
                        _issue(
                            source,
                            EnumBindingRule.ENV_READ,
                            f"environment import from os: {alias.name}",
                            statement.lineno,
                        )
                    )
    for line, text in _string_literals(tree):
        if _URL.search(text):
            issues.append(
                _issue(
                    source,
                    EnumBindingRule.URL_LITERAL,
                    "string contains a URL scheme",
                    line,
                )
            )
        if _ABSOLUTE_PATH.fullmatch(text):
            issues.append(
                _issue(
                    source,
                    EnumBindingRule.PATH_LITERAL,
                    "string is an absolute path",
                    line,
                )
            )
    return issues


def _python_issues(
    source: ModelBindingSource,
    bindings: tuple[ModelConcreteBinding, ...],
    forbidden_modules: tuple[str, ...],
) -> list[ModelBindingConformanceIssue]:
    try:
        tree = ast.parse(source.text)
    except SyntaxError as error:
        return [
            _issue(
                source,
                EnumBindingRule.SOURCE_UNPARSEABLE,
                "Python source does not parse",
                error.lineno or 0,
            )
        ]
    active_bindings = tuple(
        binding
        for binding in bindings
        if not source.path.startswith(binding.allowed_path_prefixes)
    )
    issues: list[ModelBindingConformanceIssue] = []
    for statement in ast.walk(tree):
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Call)):
            for binding in active_bindings:
                match = _concrete_match(statement, binding)
                if match is not None:
                    issues.append(
                        _issue(
                            source,
                            EnumBindingRule.CONCRETE_BINDING,
                            f"concrete binding {binding.code}: {match}",
                            statement.lineno,
                        )
                    )
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            match = next(
                (
                    module
                    for module in _import_modules(statement)
                    if _matches_module(module, forbidden_modules)
                ),
                None,
            )
            if match is not None:
                issues.append(
                    _issue(
                        source,
                        EnumBindingRule.PURE_LOADS_CONFIGURATION,
                        f"pure node imports forbidden module {match}",
                        statement.lineno,
                    )
                )
    if source.kind is EnumBindingSourceKind.HANDLER:
        issues.extend(_handler_issues(source, tree))
    return issues


def _private_name_issues(
    source: ModelBindingSource, names: tuple[ModelPrivateName, ...]
) -> list[ModelBindingConformanceIssue]:
    issues: list[ModelBindingConformanceIssue] = []
    for index, name in enumerate(names):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(name.value)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        issues.extend(
            _issue(
                source,
                EnumBindingRule.PRIVATE_NAME,
                f"private {name.kind.value} at request index {index}",
                line,
            )
            for line, text in enumerate(source.text.splitlines(), start=1)
            if pattern.search(text)
        )
    return issues


class HandlerBindingConformance:
    """Pure: the same supplied text and rules give the same ordered verdict."""

    def handle(
        self, request: ModelBindingConformanceRequest
    ) -> ModelBindingConformanceResult:
        rows = {row.node: row for row in request.protocol_table.rows}
        issues: list[ModelBindingConformanceIssue] = []
        for node in request.checked_nodes:
            row = rows.get(node)
            if row is None:
                issues.append(
                    ModelBindingConformanceIssue(
                        node=node,
                        path="",
                        line=0,
                        rule=EnumBindingRule.NO_TABLE_ROW,
                        detail="checked node has no protocol table row",
                    )
                )
                continue
            sources = tuple(source for source in request.sources if source.node == node)
            node_issues: list[ModelBindingConformanceIssue] = []
            for kind, rule in (
                (EnumBindingSourceKind.CONTRACT, EnumBindingRule.NO_CONTRACT),
                (EnumBindingSourceKind.HANDLER, EnumBindingRule.NO_SOURCE),
            ):
                if not any(source.kind is kind for source in sources):
                    node_issues.append(
                        ModelBindingConformanceIssue(
                            node=node,
                            path="",
                            line=0,
                            rule=rule,
                            detail=f"no {kind.value} source supplied",
                        )
                    )
            for source in sources:
                if source.kind is EnumBindingSourceKind.CONTRACT:
                    node_issues.extend(
                        _contract_issues(source, row.protocols, request.protocol_table)
                    )
                else:
                    node_issues.extend(
                        _python_issues(
                            source,
                            request.concrete_bindings,
                            request.pure_forbidden_modules if not row.protocols else (),
                        )
                    )
                node_issues.extend(_private_name_issues(source, request.private_names))
            node_issues.sort(
                key=lambda issue: (issue.path, issue.line, issue.rule.value)
            )
            issues.extend(node_issues)
        return ModelBindingConformanceResult(
            conforming=not issues,
            checked_nodes=request.checked_nodes,
            issues=tuple(issues),
        )
