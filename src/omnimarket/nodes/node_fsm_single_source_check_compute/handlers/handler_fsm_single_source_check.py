# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One source per state machine: the contract, never a second table in code (OMN-21477).

The contract's ``state_machine`` is the only place a machine is written. A reducer's
Python asks core's ``execute_transition`` and keeps no collection of the machine's states
or transitions. Prior art, cited and not imported: the RSD prototype kept its lifecycle
transitions both in a YAML description and in a Python mapping, so the two could drift.

The check reads two texts, the contract and the Python sources, and parses each source
with ``ast``. It fails when:

- a mapping, set, list, tuple or enum, at any scope, holds two or more of the contract's
  state names (with or without trigger names beside them);
- a function (or the module) decides in a chain of branches on two or more state literals;
- no source calls ``execute_transition``.

A state name used as a plain string (a log line, a fixture) is not a table and passes.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import yaml

from omnimarket.nodes.node_fsm_single_source_check_compute.models.model_fsm_single_source_check import (
    EnumSingleSourceIssue,
    ModelFsmSingleSourceCheckRequest,
    ModelFsmSingleSourceCheckResult,
    ModelSingleSourceIssue,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

_COLLECTIONS = (ast.Dict, ast.Set, ast.List, ast.Tuple)
_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)
_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _state_names(machine: dict[str, object]) -> tuple[str, ...]:
    """Every state name the machine declares, in contract order, once each."""
    names: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in names:
            names.append(value)

    for state in _items(machine.get("states")):
        if isinstance(state, dict):
            add(state.get("state_name"))
    add(machine.get("initial_state"))
    for key in ("terminal_states", "error_states"):
        for value in _items(machine.get(key)):
            add(value)
    for transition in _items(machine.get("transitions")):
        if isinstance(transition, dict):
            add(transition.get("from_state"))
            add(transition.get("to_state"))
    return tuple(names)


def _literal_states(node: ast.AST, states: frozenset[str]) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value in states
    }


def _is_enum_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = (
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else ""
        )
        if name.endswith("Enum") or name.endswith("Flag"):
            return True
    return False


def _enum_states(node: ast.ClassDef, states: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            value: ast.AST | None = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            targets = [statement.target.id]
            value = statement.value
        else:
            continue
        found.update(name.lower() for name in targets if name.lower() in states)
        if value is not None:
            found.update(_literal_states(value, states))
    return found


def _table_issues(
    tree: ast.AST, source: str, states: frozenset[str]
) -> Iterator[ModelSingleSourceIssue]:
    """The outermost collection or enum holding two or more state names, at any scope."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        held: set[str] = set()
        kind = ""
        if isinstance(node, _COLLECTIONS):
            held = _literal_states(node, states)
            kind = type(node).__name__.lower()
        elif isinstance(node, ast.ClassDef) and _is_enum_class(node):
            held = _enum_states(node, states)
            kind = "enum"
        if len(held) >= 2 and isinstance(node, (*_COLLECTIONS, ast.ClassDef)):
            yield ModelSingleSourceIssue(
                code=EnumSingleSourceIssue.STATE_TABLE,
                source=source,
                line=node.lineno,
                message=(
                    f"{source} line {node.lineno}: a {kind} holds the states "
                    f"{', '.join(sorted(held))} of the contract's state machine; "
                    "ask execute_transition instead of keeping a second table"
                ),
            )
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes of one scope, not entering a nested function (its own scope)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _compared_states(test: ast.AST, states: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(test):
        if isinstance(child, ast.Compare) and all(
            isinstance(op, _COMPARE_OPS) for op in child.ops
        ):
            found |= _literal_states(child, states)
    return found


def _branch_issues(
    tree: ast.AST, source: str, states: frozenset[str]
) -> Iterator[ModelSingleSourceIssue]:
    for scope in ast.walk(tree):
        if not isinstance(scope, _SCOPES):
            continue
        branches: list[tuple[int, set[str]]] = []
        for node in _scope_nodes(scope):
            if isinstance(node, ast.If):
                compared = _compared_states(node.test, states)
                if compared:
                    branches.append((node.lineno, compared))
            elif isinstance(node, ast.Match):
                cases = {
                    pattern.value.value
                    for case in node.cases
                    for pattern in ast.walk(case.pattern)
                    if isinstance(pattern, ast.MatchValue)
                    and isinstance(pattern.value, ast.Constant)
                    and pattern.value.value in states
                }
                if cases:
                    branches.append((node.lineno, cases))
        distinct = set().union(*(held for _, held in branches)) if branches else set()
        if len(branches) >= 2 and len(distinct) >= 2:
            line = min(lineno for lineno, _ in branches)
            yield ModelSingleSourceIssue(
                code=EnumSingleSourceIssue.STATE_BRANCH_CHAIN,
                source=source,
                line=line,
                message=(
                    f"{source} line {line}: a chain of branches compares the states "
                    f"{', '.join(sorted(distinct))}; the contract decides the "
                    "transition, ask execute_transition"
                ),
            )


def _calls_execute_transition(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "execute_transition") or (
                isinstance(func, ast.Attribute) and func.attr == "execute_transition"
            ):
                return True
    return False


class HandlerFsmSingleSourceCheck:
    """``handle(ModelFsmSingleSourceCheckRequest) -> ModelFsmSingleSourceCheckResult``."""

    def handle(
        self, request: ModelFsmSingleSourceCheckRequest
    ) -> ModelFsmSingleSourceCheckResult:
        issues: list[ModelSingleSourceIssue] = []
        names = self._read_state_names(request.contract_text, issues)
        states = frozenset(names)
        parsed: list[tuple[str, ast.AST]] = []
        for source in request.sources:
            try:
                parsed.append((source.name, ast.parse(source.text)))
            except SyntaxError as error:
                issues.append(
                    ModelSingleSourceIssue(
                        code=EnumSingleSourceIssue.SOURCE_UNPARSEABLE,
                        source=source.name,
                        line=error.lineno,
                        message=f"{source.name}: not valid Python ({error.msg})",
                    )
                )
        for name, tree in parsed:
            if not states:
                break
            found = [
                *_table_issues(tree, name, states),
                *_branch_issues(tree, name, states),
            ]
            issues.extend(sorted(found, key=lambda issue: issue.line or 0))
        all_parsed = len(parsed) == len(request.sources)
        if all_parsed and not any(_calls_execute_transition(t) for _, t in parsed):
            issues.append(
                ModelSingleSourceIssue(
                    code=EnumSingleSourceIssue.EXECUTE_TRANSITION_MISSING,
                    source=request.sources[0].name if request.sources else "",
                    message=(
                        "no source calls execute_transition: the reducer's handler "
                        "must run its transitions through core's executor"
                    ),
                )
            )
        return ModelFsmSingleSourceCheckResult(
            issues=tuple(issues),
            exit_code=1 if issues else 0,
            state_names=names,
        )

    @staticmethod
    def _read_state_names(
        contract_text: str, issues: list[ModelSingleSourceIssue]
    ) -> tuple[str, ...]:
        try:
            document = yaml.safe_load(contract_text)
        except yaml.YAMLError as error:
            issues.append(
                ModelSingleSourceIssue(
                    code=EnumSingleSourceIssue.CONTRACT_UNREADABLE,
                    source="contract",
                    message=f"contract: not valid YAML ({type(error).__name__})",
                )
            )
            return ()
        machine = document.get("state_machine") if isinstance(document, dict) else None
        names = _state_names(machine) if isinstance(machine, dict) else ()
        if not names:
            issues.append(
                ModelSingleSourceIssue(
                    code=EnumSingleSourceIssue.STATE_MACHINE_MISSING,
                    source="contract",
                    message="contract: declares no state_machine with states",
                )
            )
        return names
