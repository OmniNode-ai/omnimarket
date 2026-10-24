#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: detect Handler* classes with zero import references (OMN-10821).

A Handler class defined in handler_*.py that is never imported anywhere outside
its own file cannot be invoked by the DI container, plugin system, or consumer.
These are dead handlers — if they are meant to be active, they are silently absent
from the pipeline.

This check scans src/omnimarket/nodes/ for Handler* class definitions, then
verifies that each class is imported at least once in a file other than the one
that defines it. Files with an inline ``# unimported-handler-allow: <reason>``
annotation are skipped.

The existing validate-no-transport-imports hook uses ``--transport-imports``
for the core static policy plus dynamic imports throughout node code. Dynamic
targets are checked when their module names or namespace prefixes can be
resolved statically. No new dynamic-import exceptions or baseline entries are admitted.

Exit codes: 0 = clean; 1 = violations found; 2 = invocation error.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import pathlib
import re
import sys

_INLINE_ALLOW_MARKER = "# unimported-handler-allow:"
_HANDLER_CLASS_RE = re.compile(r"^Handler[A-Z]\w*$")


def _collect_handler_definitions(
    src_root: pathlib.Path,
) -> list[tuple[str, pathlib.Path, int]]:
    """Return (ClassName, defining_file, lineno) for all Handler* classes."""
    definitions: list[tuple[str, pathlib.Path, int]] = []
    for py_file in sorted(src_root.rglob("handler_*.py")):
        # Skip test files
        if py_file.name.startswith("test_") or "conftest" in py_file.name:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if _INLINE_ALLOW_MARKER in source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _HANDLER_CLASS_RE.match(node.name):
                definitions.append((node.name, py_file, node.lineno))
    return definitions


def _collect_all_source_lines(src_root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Return {path: source_text} for all .py files under src_root."""
    sources: dict[pathlib.Path, str] = {}
    for py_file in sorted(src_root.rglob("*.py")):
        with contextlib.suppress(OSError):
            sources[py_file] = py_file.read_text(encoding="utf-8")
    return sources


def _repo_relative(path: pathlib.Path, repo_root: pathlib.Path) -> pathlib.Path:
    with contextlib.suppress(ValueError):
        return path.relative_to(repo_root)
    return path


def _load_baseline(baseline_path: pathlib.Path | None) -> set[str]:
    """Load documented legacy entries as ``repo/path.py:HandlerClass`` keys."""
    if baseline_path is None or not baseline_path.is_file():
        return set()

    entries: set[str] = set()
    for raw_line in baseline_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line.split("#", 1)[0].strip())
    return entries


def _collect_referenced_symbols(source: str) -> set[str]:
    """Return Python symbols imported or referenced by AST nodes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols.add(alias.asname or alias.name.rsplit(".", 1)[-1])
            continue

        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                symbols.add(alias.asname or alias.name)
            continue

        if isinstance(node, ast.Name):
            symbols.add(node.id)
            continue

        if isinstance(node, ast.Attribute):
            symbols.add(node.attr)

    return symbols


def find_dead_handlers(
    src_root: pathlib.Path,
    baseline_path: pathlib.Path | None = None,
) -> list[str]:
    """Return sorted violation strings for Handler* with no wiring evidence."""
    definitions = _collect_handler_definitions(src_root)
    if not definitions:
        return []

    all_sources = _collect_all_source_lines(src_root)
    all_source_symbols = {
        py_file: _collect_referenced_symbols(source)
        for py_file, source in all_sources.items()
    }
    repo_root = src_root.parent.parent
    baseline = _load_baseline(baseline_path)

    dead: list[str] = []
    for class_name, defining_file, lineno in sorted(definitions):
        imported_elsewhere = False
        for py_file, symbols in all_source_symbols.items():
            if py_file == defining_file:
                continue
            if class_name in symbols:
                imported_elsewhere = True
                break
        if not imported_elsewhere:
            rel = _repo_relative(defining_file, repo_root)
            baseline_key = f"{rel}:{class_name}"
            if baseline_key in baseline:
                continue
            dead.append(f"{rel}:{lineno}: {class_name}")

    return dead


# The transport hook uses this existing import gate's CLI, so both import checks
# share one implementation file. The core validator
# continues to own static transport policy; dynamic resolution is checked here.
_DYNAMIC_DRIVERS = frozenset(
    {
        "psycopg",
        "psycopg2",
        "asyncpg",
        "sqlalchemy",
        "kafka",
        "aiokafka",
        "confluent_kafka",
        "redis",
    }
)
_DYNAMIC_APIS = frozenset(
    {
        "importlib.import_module",
        "importlib.__import__",
        "__import__",
        "builtins.__import__",
        "importlib.util.find_spec",
        "importlib.util.spec_from_file_location",
        "importlib.util.spec_from_loader",
    }
)
_STATIC_EXCLUDES = frozenset(
    {
        "node_merge_sweep_compute/__main__.py",
        "node_merge_sweep_compute/consumer.py",
        "node_navigation_history_reducer/handlers/handler_navigation_history_reducer.py",
    }
)


def _qualified_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, aliases)}.{node.attr}"
    return ""


def _string_value(node: ast.expr | None, strings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_value(node.left, strings)
        right = _string_value(node.right, strings)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            value = _string_value(
                part.value if isinstance(part, ast.FormattedValue) else part, strings
            )
            parts.append(value if value is not None else "<dynamic>")
        return "".join(parts)
    return None


def _declared_effect_drivers(path: pathlib.Path) -> set[str]:
    import yaml

    node_dir = next(
        (parent for parent in path.parents if parent.name.startswith("node_")), None
    )
    if node_dir is None:
        return set()
    metadata = node_dir / "metadata.yaml"
    if not metadata.is_file():
        return set()
    document = yaml.safe_load(metadata.read_text(encoding="utf-8"))
    kind = document.get("node_role") if isinstance(document, dict) else None
    contract_path = node_dir / "contract.yaml"
    if not kind and contract_path.is_file():
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        kind = contract.get("node_type") if isinstance(contract, dict) else None
    # The original static hook selects pure nodes by path. Metadata cannot
    # grant a driver allowance that the static policy would refuse there.
    if node_dir.name.endswith(("_compute", "_reducer")):
        return set()
    is_effect = kind == "effect" if kind else node_dir.name.endswith("_effect")
    if not is_effect:
        return set()
    dependencies = (
        document.get("dependencies", []) if isinstance(document, dict) else []
    )
    packages = {
        re.split(r"[<>=!~\[ ;]", dep, maxsplit=1)[0].replace("-", "_")
        for dep in dependencies
        if isinstance(dep, str)
    }
    if "psycopg2_binary" in packages:
        packages.add("psycopg2")
    if "kafka_python" in packages:
        packages.add("kafka")
    return packages & _DYNAMIC_DRIVERS


class _DynamicImports(ast.NodeVisitor):
    def __init__(self, path: pathlib.Path, tree: ast.Module) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.strings: dict[str, str] = {}
        self.calls: list[tuple[int, str, str]] = []
        self.allowed_drivers = _declared_effect_drivers(path)
        # Module constants/import aliases are available to functions even when
        # declared below them. Reassigned globals cannot be proved constant.
        assignments: dict[str, int] = {}
        self.global_candidates: dict[str, set[str]] = {}
        for statement in tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self.visit(statement)
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                self._assign(statement)
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = assignments.get(target.id, 0) + 1
                        value = _string_value(statement.value, self.strings)
                        if value is not None:
                            self.global_candidates.setdefault(target.id, set()).add(
                                value
                            )
        self.uncertain_globals = {
            name for name, count in assignments.items() if count > 1
        }
        self.module_strings = self.strings.copy()
        for name in self.uncertain_globals:
            self.module_strings.pop(name, None)

    def _assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = _string_value(node.value, self.strings)
        for target in targets:
            if isinstance(target, ast.Name):
                self.strings.pop(target.id, None)
                if value is not None:
                    self.strings[target.id] = value

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node)
        # Also follow aliases such as load = importlib.import_module.
        name = _qualified_name(node.value, self.aliases)
        for target in node.targets:
            if isinstance(target, ast.Name) and name in _DYNAMIC_APIS:
                self.aliases[target.id] = name
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        aliases, strings = self.aliases.copy(), self.strings.copy()
        self.strings.update(self.module_strings)
        for name in self.uncertain_globals:
            self.strings.pop(name, None)
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]:
            self.strings.pop(argument.arg, None)
        self.generic_visit(node)
        self.aliases, self.strings = aliases, strings

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        api = _qualified_name(node.func, self.aliases)
        if api in _DYNAMIC_APIS or api.startswith("importlib.util.spec_from_"):
            target = (
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
            )
            module = _string_value(target, self.strings)
            if module is not None and module.startswith("."):
                package = (
                    node.args[1]
                    if len(node.args) > 1
                    else next(
                        (kw.value for kw in node.keywords if kw.arg == "package"), None
                    )
                )
                package_name = _string_value(package, self.strings)
                if package_name is not None:
                    from importlib.util import resolve_name

                    try:
                        module = resolve_name(module, package_name)
                    except (ImportError, ValueError):
                        module = None
                else:
                    module = None
            targets = [module]
            if module is None and isinstance(target, ast.Name):
                targets.extend(self.global_candidates.get(target.id, ()))
            if api == "importlib.util.spec_from_file_location":
                location = (
                    node.args[1]
                    if len(node.args) > 1
                    else next(
                        (kw.value for kw in node.keywords if kw.arg == "location"), None
                    )
                )
                location_name = _string_value(location, self.strings)
                if location_name is None:
                    targets.append(None)
                else:
                    normalized = location_name.replace("\\", "/").replace("/", ".")
                    marker = normalized.find("omnimarket.nodes.node_")
                    if marker >= 0:
                        targets.append(normalized[marker:])
                    else:
                        for driver_name in _DYNAMIC_DRIVERS:
                            if driver_name in normalized.split("."):
                                targets.append(driver_name)
                                break
            own_node = next((p for p in self.path.parts if p.startswith("node_")), "")
            refused = []
            for candidate in targets:
                cross_node = (
                    candidate is not None
                    and candidate.startswith("omnimarket.nodes.")
                    and (
                        candidate.split(".")[2].startswith("node_")
                        or "<dynamic>" in candidate.split(".")[2]
                    )
                    and candidate.split(".")[2] != own_node
                )
                driver = (
                    candidate is not None
                    and candidate.split(".")[0] in _DYNAMIC_DRIVERS
                )
                # Only declared effect drivers inherit the existing static allowance.
                if cross_node or (
                    driver and candidate.split(".")[0] not in self.allowed_drivers
                ):
                    refused.append(candidate or "unresolved target")
            if refused:
                reason = ", ".join(dict.fromkeys(refused))
                fingerprint = f"{api}|{ast.dump(node)}|{reason}"
                digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
                self.calls.append((node.lineno, reason, digest))
        self.generic_visit(node)


def dynamic_import_violations(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Inspect node source without importing it, including handler helpers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    checker = _DynamicImports(path, tree)
    checker.visit(tree)
    counts: dict[str, int] = {}
    calls = []
    for line, reason, digest in checker.calls:
        counts[digest] = counts.get(digest, 0) + 1
        calls.append((line, reason, f"{digest}.{counts[digest]}"))
    return calls


def transport_import_main(paths: list[str]) -> int:
    """The existing validate-no-transport-imports hook and CI entrypoint."""
    from omnibase_core.validation.validator_transport_import import (
        ValidatorTransportImport,
    )

    root = pathlib.Path(__file__).resolve().parents[2]
    nodes = root / "src/omnimarket/nodes"
    # Explicit paths are useful to pre-commit and fixture probes; no args is the
    # same full-tree census CI runs. Do not let old static exclusions hide calls.
    candidates = [pathlib.Path(path) for path in paths] if paths else [nodes]
    files: set[pathlib.Path] = set()
    for candidate in candidates:
        if not candidate.exists():
            print(f"ERROR: missing transport scan path: {candidate}")
            return 2
        files.update(candidate.rglob("*.py") if candidate.is_dir() else [candidate])
    errors = []
    validator = ValidatorTransportImport()
    for path in sorted(files):
        if (
            path.suffix != ".py"
            or "tests" in path.parts
            or path.name.startswith("test_")
        ):
            continue
        node_index = next(
            (i for i, part in enumerate(path.parts) if part.startswith("node_")), None
        )
        if node_index is None:
            continue
        rel = _repo_relative(path, root).as_posix()
        node_rel = pathlib.Path(*path.parts[node_index:]).as_posix()
        if (
            path.parts[node_index].endswith(("_compute", "_reducer"))
            and node_rel not in _STATIC_EXCLUDES
        ):
            errors.extend(str(v) for v in validator.check_file(path))
        try:
            violations = dynamic_import_violations(path)
        except (SyntaxError, OSError, UnicodeError) as exc:
            errors.append(
                f"{rel}: cannot inspect transport imports: {type(exc).__name__}"
            )
            continue
        for line, target, _digest in violations:
            errors.append(f"{rel}:{line}: dynamic transport/node import: {target}")
    for error in errors:
        print(error)
    print(f"Transport imports: {len(errors)} violation(s)")
    return 1 if errors else 0


def main() -> int:
    if sys.argv[1:2] == ["--transport-imports"]:
        return transport_import_main(sys.argv[2:])
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src_root = repo_root / "src" / "omnimarket"
    baseline_path = (
        repo_root / "scripts" / "validation" / "unimported_handler_baseline.txt"
    )
    if not src_root.is_dir():
        print(f"ERROR: src/omnimarket not found under {repo_root}", file=sys.stderr)
        return 2

    dead = find_dead_handlers(src_root, baseline_path=baseline_path)
    if dead:
        print(f"ERROR: {len(dead)} Handler class(es) with no wiring evidence:")
        for entry in dead:
            print(f"  {entry}")
        print()
        print(
            "Fix: import the handler in a Python module, or add a documented "
            "legacy entry to scripts/validation/unimported_handler_baseline.txt."
        )
        return 1

    print(
        "OK: all Handler* classes in src/omnimarket/nodes/ are imported, "
        "or explicitly baselined."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
