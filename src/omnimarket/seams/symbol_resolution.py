# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Project-wide symbol/constant resolution for the seam-graph extractor
(OMN-15779, AC1 residual of OMN-15763).

``extraction.py``'s code-level observers (``producer_send``/
``consumer_subscribe``) only ever recognized a literal ``ast.Constant``
string argument. Real corpus call sites almost never pass one — they pass a
``Name`` or ``Attribute`` that was assigned earlier (``self._topic``, a
module constant, a class attribute, or a dict-literal-backed registry
``.resolve(key)``/``.get(key)`` lookup). This module builds a **pure,
deterministic, project-wide symbol table** from the already-parsed source
tree (no imports are executed, no live I/O — resolution is a static
read-back over ASTs already produced for the corpus being scanned) and
resolves a call argument expression back to the literal string it denotes,
when that is statically determinable.

Two-phase build, both over the SAME parsed ASTs the extractor already has:

1. :func:`build_project_index` walks every file once to collect module-level
   constants/aliases, class-body constants/aliases, ``self.attr = <expr>``
   assignments per class, import bindings, and every dict-literal node in
   the tree (a "constant table" candidate — this is how the
   ``ServiceTopicRegistry.from_defaults().resolve(topic_keys.X)`` idiom,
   the dominant real-corpus shape, resolves: ``from_defaults()`` builds a
   dict literal mapping key constants to value constants, and ``resolve()``
   is a lookup into it).
2. Every dict literal's keys/values are then resolved ONCE into a global
   ``key_index`` (``key literal -> [(value literal, defining file)]``), so a
   ``.resolve(KEY)``/``.get(KEY)``/``[KEY]`` call site only needs to resolve
   its own key argument and do an O(1) lookup — not re-walk every dict in
   the corpus per call site.

Resolution is intentionally conservative and never fabricates:

* Depth-bounded (``_MAX_RESOLVE_DEPTH``) to guarantee termination on
  reference cycles.
* An **ambiguous** dict-key match (two dict literals mapping the same
  resolved key to two different resolved values) resolves to ``None``
  rather than guessing — a wrong pick would poison the graph silently,
  which is worse than an honest miss.
* Dynamic shapes (f-strings, ``%``/``.format`` templating, arbitrary
  function calls whose return value isn't itself a modeled lookup,
  ``os.environ``-derived values) are explicitly OUT OF SCOPE and resolve to
  ``None`` — the caller (``extraction.py``) turns that into an explicit
  UNRESOLVED-class observation, never a silent miss and never a fabricated
  literal.
* Cross-file resolution only follows *module-level* and *class-body-level*
  bindings (the shapes an ``import`` can actually name) — it never chases a
  ``self.attr`` or function-local assignment into another file, since an
  import cannot bind to instance state or a local variable.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

__all__ = [
    "ProjectIndex",
    "build_project_index",
    "resolve_topic_expr",
]

_MAX_RESOLVE_DEPTH = 8

# A resolved terminal is either a string Constant, a List (so the caller can
# unwrap ``[TOPIC]``-style single-element list arguments), or a Dict (so a
# resolved-to-a-dict-literal reference can itself be looked into). Paired
# with the repo-relative file the terminal node actually lives in, since a
# resolution chain may cross files via an import.
_ResolvedTerminal = tuple[ast.expr, str]


@dataclass
class ModuleSymbols:
    """Symbols collected from one parsed Python file, by direct AST walk."""

    parent_map: dict[ast.AST, ast.AST] = field(default_factory=dict)
    module_constants: dict[str, str] = field(default_factory=dict)
    module_aliases: dict[str, ast.expr] = field(default_factory=dict)
    class_constants: dict[str, dict[str, str]] = field(default_factory=dict)
    class_aliases: dict[str, dict[str, ast.expr]] = field(default_factory=dict)
    self_attr_exprs: dict[str, dict[str, list[ast.expr]]] = field(default_factory=dict)
    imports: dict[str, str] = field(default_factory=dict)
    from_imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    dict_literals: list[ast.Dict] = field(default_factory=list)


@dataclass
class ProjectIndex:
    """Corpus-wide symbol index used to resolve a code-level reference."""

    by_file: dict[str, ModuleSymbols] = field(default_factory=dict)
    by_module_path: dict[str, str] = field(default_factory=dict)
    key_index: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _dotted_module_path(repo_relative: str) -> str:
    """Best-effort dotted module path for a ``src/<pkg>/...`` layout file.

    Every repo in the traced corpus (``omnibase_infra``, ``omnimarket``,
    ``omnibase_core``) follows the standard ``src/<package>/...`` layout, so
    an import target's dotted path (``omnibase_infra.topics.topic_keys``)
    maps directly onto ``src/omnibase_infra/topics/topic_keys.py`` by
    stripping the ``src/`` prefix and the ``.py`` suffix. Files outside
    ``src/`` (scripts, tests) are not import targets in this scheme and are
    keyed by their own repo-relative path with the same transform — they
    simply will not collide with a real dotted import path.
    """

    posix = repo_relative.replace("\\", "/")
    marker = "src/"
    idx = posix.rfind(marker)
    tail = posix[idx + len(marker) :] if idx != -1 else posix
    if tail.endswith(".py"):
        tail = tail[: -len(".py")]
    dotted = tail.replace("/", ".")
    if dotted.endswith(".__init__"):
        dotted = dotted[: -len(".__init__")]
    return dotted


def _record_binding(
    name: str,
    value_expr: ast.expr,
    constants: dict[str, str],
    aliases: dict[str, ast.expr],
) -> None:
    if isinstance(value_expr, ast.Constant) and isinstance(value_expr.value, str):
        constants[name] = value_expr.value
    else:
        aliases[name] = value_expr


def _collect_class_symbols(
    class_def: ast.ClassDef,
) -> tuple[dict[str, str], dict[str, ast.expr], dict[str, list[ast.expr]]]:
    constants: dict[str, str] = {}
    aliases: dict[str, ast.expr] = {}
    for stmt in class_def.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    _record_binding(target.id, stmt.value, constants, aliases)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and isinstance(stmt.target, ast.Name)
        ):
            _record_binding(stmt.target.id, stmt.value, constants, aliases)

    self_attrs: dict[str, list[ast.expr]] = {}
    for node in ast.walk(class_def):
        self_target: ast.expr | None = None
        self_value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            self_target = node.targets[0]
            self_value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self_target = node.target
            self_value = node.value
        if (
            self_target is not None
            and self_value is not None
            and isinstance(self_target, ast.Attribute)
            and isinstance(self_target.value, ast.Name)
            and self_target.value.id == "self"
        ):
            self_attrs.setdefault(self_target.attr, []).append(self_value)
    return constants, aliases, self_attrs


def _collect_module_symbols(tree: ast.Module) -> ModuleSymbols:
    symbols = ModuleSymbols()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    _record_binding(
                        target.id,
                        stmt.value,
                        symbols.module_constants,
                        symbols.module_aliases,
                    )
        elif (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and isinstance(stmt.target, ast.Name)
        ):
            _record_binding(
                stmt.target.id,
                stmt.value,
                symbols.module_constants,
                symbols.module_aliases,
            )
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                local_name = alias.asname or alias.name.split(".")[0]
                symbols.imports[local_name] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module is None:
                # A relative "from . import x" without a resolvable dotted
                # module — out of scope, left unresolved rather than guessed.
                continue
            for alias in stmt.names:
                local_name = alias.asname or alias.name
                symbols.from_imports[local_name] = (stmt.module, alias.name)
        elif isinstance(stmt, ast.ClassDef):
            constants, aliases, self_attrs = _collect_class_symbols(stmt)
            symbols.class_constants[stmt.name] = constants
            symbols.class_aliases[stmt.name] = aliases
            symbols.self_attr_exprs[stmt.name] = self_attrs

    symbols.dict_literals = [
        node for node in ast.walk(tree) if isinstance(node, ast.Dict)
    ]
    return symbols


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node
    return parent_map


def _enclosing_function(
    node: ast.AST, parent_map: dict[ast.AST, ast.AST]
) -> ast.AST | None:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parent_map.get(current)
    return None


def _enclosing_class_name(
    node: ast.AST, parent_map: dict[ast.AST, ast.AST]
) -> str | None:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, ast.ClassDef):
            return current.name
        current = parent_map.get(current)
    return None


def _find_local_assignment(
    name_node: ast.Name, name: str, parent_map: dict[ast.AST, ast.AST]
) -> ast.expr | None:
    """Best-effort function-local resolution: the nearest textually-preceding
    ``name = <expr>`` assignment within the enclosing function. Not
    control-flow-sensitive (a branch's assignment is treated the same as a
    straight-line one) — a documented best-effort limitation, matching this
    module's existing "best-effort" precedent elsewhere in the extractor."""

    func = _enclosing_function(name_node, parent_map)
    if func is None:
        return None
    candidates: list[ast.expr] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    candidates.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            candidates.append(node.value)
    if not candidates:
        return None
    usage_line = getattr(name_node, "lineno", 0) or 0
    preceding = [c for c in candidates if getattr(c, "lineno", 0) <= usage_line]
    return (preceding or candidates)[-1]


def build_project_index(
    parsed: dict[str, tuple[ast.AST | None, str]],
) -> ProjectIndex:
    """Build the corpus-wide index from already-parsed ``{file: (tree, text)}``.

    Pure function over in-memory ASTs — no filesystem access, no imports
    executed. Deterministic given a deterministic iteration order of
    ``parsed`` (the caller passes an already-sorted mapping)."""

    idx = ProjectIndex()
    for file_key, (tree, _text) in parsed.items():
        if tree is None or not isinstance(tree, ast.Module):
            continue
        symbols = _collect_module_symbols(tree)
        symbols.parent_map = _build_parent_map(tree)
        idx.by_file[file_key] = symbols
        module_path = _dotted_module_path(file_key)
        idx.by_module_path.setdefault(module_path, file_key)

    idx.key_index = _build_key_index(idx)
    return idx


def _build_key_index(idx: ProjectIndex) -> dict[str, list[tuple[str, str]]]:
    key_index: dict[str, list[tuple[str, str]]] = {}
    for file_key in sorted(idx.by_file):
        symbols = idx.by_file[file_key]
        ordered_dicts = sorted(
            symbols.dict_literals,
            key=lambda d: (getattr(d, "lineno", 0), getattr(d, "col_offset", 0)),
        )
        for dict_node in ordered_dicts:
            for key_expr, value_expr in zip(
                dict_node.keys, dict_node.values, strict=True
            ):
                if key_expr is None or value_expr is None:
                    continue
                key_result = _resolve_expr(key_expr, file_key, idx, 0)
                if key_result is None:
                    continue
                key_node, _key_file = key_result
                if not (
                    isinstance(key_node, ast.Constant)
                    and isinstance(key_node.value, str)
                ):
                    continue
                value_result = _resolve_expr(value_expr, file_key, idx, 0)
                if value_result is None:
                    continue
                value_node, value_file = value_result
                if not (
                    isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, str)
                ):
                    continue
                key_index.setdefault(key_node.value, []).append(
                    (value_node.value, value_file)
                )
    return key_index


def _resolve_expr(
    expr: ast.expr, file_key: str, idx: ProjectIndex, depth: int
) -> _ResolvedTerminal | None:
    if depth > _MAX_RESOLVE_DEPTH:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return (expr, file_key)
    if isinstance(expr, ast.List | ast.Dict):
        return (expr, file_key)
    if isinstance(expr, ast.Name):
        return _resolve_name(expr, file_key, idx, depth)
    if isinstance(expr, ast.Attribute):
        return _resolve_attribute(expr, file_key, idx, depth)
    if isinstance(expr, ast.Call | ast.Subscript):
        return _resolve_lookup(expr, file_key, idx, depth)
    return None


def _resolve_name(
    expr: ast.Name, file_key: str, idx: ProjectIndex, depth: int
) -> _ResolvedTerminal | None:
    symbols = idx.by_file.get(file_key)
    if symbols is None:
        return None
    name = expr.id

    local_expr = _find_local_assignment(expr, name, symbols.parent_map)
    if local_expr is not None:
        result = _resolve_expr(local_expr, file_key, idx, depth + 1)
        if result is not None:
            return result

    if name in symbols.module_constants:
        return (ast.Constant(value=symbols.module_constants[name]), file_key)
    if name in symbols.module_aliases:
        return _resolve_expr(symbols.module_aliases[name], file_key, idx, depth + 1)

    if name in symbols.from_imports:
        module_path, original = symbols.from_imports[name]
        target_file = idx.by_module_path.get(module_path)
        if target_file is None:
            return None
        target_symbols = idx.by_file[target_file]
        if original in target_symbols.module_constants:
            return (
                ast.Constant(value=target_symbols.module_constants[original]),
                target_file,
            )
        if original in target_symbols.module_aliases:
            return _resolve_expr(
                target_symbols.module_aliases[original], target_file, idx, depth + 1
            )
    return None


def _resolve_attribute(
    expr: ast.Attribute, file_key: str, idx: ProjectIndex, depth: int
) -> _ResolvedTerminal | None:
    symbols = idx.by_file.get(file_key)
    if symbols is None:
        return None
    base = expr.value
    attr = expr.attr

    if isinstance(base, ast.Name) and base.id == "self":
        class_name = _enclosing_class_name(expr, symbols.parent_map)
        if class_name is None:
            return None
        candidates = symbols.self_attr_exprs.get(class_name, {}).get(attr, [])
        for candidate in sorted(
            candidates,
            key=lambda c: (getattr(c, "lineno", 0), getattr(c, "col_offset", 0)),
        ):
            result = _resolve_expr(candidate, file_key, idx, depth + 1)
            if result is not None:
                return result
        return None

    if not isinstance(base, ast.Name):
        return None
    base_name = base.id

    # module.CONST — base is bound to an imported module.
    if base_name in symbols.imports:
        module_path = symbols.imports[base_name]
        target_file = idx.by_module_path.get(module_path)
        if target_file is not None:
            target_symbols = idx.by_file[target_file]
            if attr in target_symbols.module_constants:
                return (
                    ast.Constant(value=target_symbols.module_constants[attr]),
                    target_file,
                )
            if attr in target_symbols.module_aliases:
                return _resolve_expr(
                    target_symbols.module_aliases[attr], target_file, idx, depth + 1
                )

    # ClassName.ATTR — class defined locally in this file.
    if attr in symbols.class_constants.get(base_name, {}):
        return (
            ast.Constant(value=symbols.class_constants[base_name][attr]),
            file_key,
        )
    if attr in symbols.class_aliases.get(base_name, {}):
        return _resolve_expr(
            symbols.class_aliases[base_name][attr], file_key, idx, depth + 1
        )

    # base_name bound via `from <pkg> import <name>` — two distinct real
    # shapes share this AST form and both must be tried:
    #   (a) ClassName imported from another file: `from mod import ClassName`
    #   (b) a SUBMODULE imported from a package: `from pkg import submodule`
    #       (e.g. `from omnibase_infra.topics import topic_keys`) — endemic
    #       in the real corpus; statically indistinguishable from (a) without
    #       resolving what `original` actually names, so both are tried.
    if base_name in symbols.from_imports:
        module_path, original = symbols.from_imports[base_name]
        target_file = idx.by_module_path.get(module_path)
        if target_file is not None:
            target_symbols = idx.by_file[target_file]
            if attr in target_symbols.class_constants.get(original, {}):
                return (
                    ast.Constant(value=target_symbols.class_constants[original][attr]),
                    target_file,
                )
        submodule_path = f"{module_path}.{original}"
        submodule_file = idx.by_module_path.get(submodule_path)
        if submodule_file is not None:
            submodule_symbols = idx.by_file[submodule_file]
            if attr in submodule_symbols.module_constants:
                return (
                    ast.Constant(value=submodule_symbols.module_constants[attr]),
                    submodule_file,
                )
            if attr in submodule_symbols.module_aliases:
                return _resolve_expr(
                    submodule_symbols.module_aliases[attr],
                    submodule_file,
                    idx,
                    depth + 1,
                )
    return None


def _resolve_lookup(
    expr: ast.Call | ast.Subscript, file_key: str, idx: ProjectIndex, depth: int
) -> _ResolvedTerminal | None:
    """``<obj>.resolve(KEY)`` / ``<obj>.get(KEY)`` / ``<obj>[KEY]`` — a
    registry-style lookup. Rather than trace the exact object identity back
    to its defining dict literal (a much larger constructor/DI-flow
    analysis), this resolves the KEY argument to a literal and searches the
    project-wide ``key_index`` of already-resolved dict-literal entries for
    a match. An ambiguous match (the same key literal resolves to more than
    one distinct value somewhere in the corpus) is refused, not guessed."""

    if isinstance(expr, ast.Call):
        func = expr.func
        if not isinstance(func, ast.Attribute) or func.attr not in {
            "resolve",
            "get",
        }:
            return None
        if len(expr.args) != 1:
            return None
        key_expr = expr.args[0]
    else:
        key_expr = expr.slice

    key_result = _resolve_expr(key_expr, file_key, idx, depth + 1)
    if key_result is None:
        return None
    key_node, _key_file = key_result
    if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
        return None

    candidates = idx.key_index.get(key_node.value, [])
    if not candidates:
        return None
    distinct_values = {value for value, _file in candidates}
    if len(distinct_values) > 1:
        return None
    value, value_file = sorted(candidates, key=lambda c: c[1])[0]
    return (ast.Constant(value=value), value_file)


def resolve_topic_expr(
    expr: ast.expr, file_key: str, project_index: ProjectIndex
) -> str | None:
    """Resolve a call-argument expression to the literal string it denotes,
    or ``None`` if it is not statically determinable. Unwraps a single
    resolved ``[TOPIC]`` list argument (the ``consumer.subscribe([TOPIC])``
    idiom) one level, matching the pre-existing literal-list handling."""

    result = _resolve_expr(expr, file_key, project_index, 0)
    if result is None:
        return None
    node, node_file = result
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.List) and node.elts:
        return resolve_topic_expr(node.elts[0], node_file, project_index)
    return None
