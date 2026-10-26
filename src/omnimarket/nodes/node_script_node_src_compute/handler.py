"""Which node source a plugin script runs against (OMN-17427).

2026-10-18 ~21:40Z: the 0.4.437 plugin cache held the throughput tick from the change that calls
``HandlerProcessFixOwnership`` and a bundled ``src/`` from before that node existed. The script took the first source
that held a sibling node and crashed with ``AttributeError`` on its first process-fix line, so the orchestrator saw no
headroom. The reading is static (the script's imports against the source files' top-level names), so a candidate that
lacks a name is skipped without importing it, and the plugin validator runs the same reading over the commit a script
ships in.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable
from pathlib import Path

from .models import ModelScriptNodeSrcRequest, ModelScriptNodeSrcResult

NODE_PACKAGES = ("omniclaude_internal", "omni")


# The reader is injected: the node reads the script and the candidate sources through it.
Reader = Callable[[Path], str]
READ_UTF8: Reader = functools.partial(Path.read_text, encoding="utf-8")


def module_path(src: Path, dotted: str) -> Path | None:
    base = src.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return (
        base if base.is_dir() else None
    )  # a namespace package: its names are its submodules


def module_names(path: Path, read: Reader = READ_UTF8) -> set[str] | None:
    """The top-level names a module defines; None when they cannot be known statically (a star import, ``__getattr__``)."""
    if path.is_dir():
        return set()
    try:
        tree = ast.parse(read(path))
    except (OSError, SyntaxError):
        return None
    names: set[str] = set()
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(
                n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)
            )
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return None
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If | ast.Try | ast.With):
            pending += [
                *node.body,
                *getattr(node, "orelse", []),
                *getattr(node, "finalbody", []),
            ]
            pending += [s for h in getattr(node, "handlers", []) for s in h.body]
    return None if "__getattr__" in names else names


def unresolved(
    tree: ast.AST, src: Path, read: Reader = READ_UTF8
) -> list[tuple[int, str]]:
    """(line, what) for every node module or name the script takes that ``src`` does not define."""
    bad: list[tuple[int, str]] = []

    def module_ok(dotted: str, line: int) -> Path | None:
        found = module_path(src, dotted)
        if found is None:
            bad.append((line, f"module {dotted}"))
        return found

    def attr_ok(dotted: str, found: Path, attr: str, line: int) -> None:
        names = module_names(found, read)
        if (
            names is not None
            and attr not in names
            and module_path(src, f"{dotted}.{attr}") is None
        ):
            bad.append((line, f"{dotted}.{attr}"))

    def literal_module(call: ast.expr) -> str | None:
        if not (isinstance(call, ast.Call) and call.args):
            return None
        func, arg = call.func, call.args[0]
        named = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        ) == "import_module"
        if not (named and isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            return None
        return arg.value if arg.value.split(".")[0] in NODE_PACKAGES else None

    aliases: dict[str, tuple[str, Path]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] in NODE_PACKAGES
        ):
            found = module_ok(node.module or "", node.lineno)
            if found is not None:
                for alias in node.names:
                    attr_ok(node.module or "", found, alias.name, node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in NODE_PACKAGES:
                    module_ok(alias.name, node.lineno)
        elif (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and node.value is not None
            and (dotted := literal_module(node.value)) is not None
        ):
            found = module_ok(dotted, node.lineno)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if found is not None:
                aliases.update(
                    {t.id: (dotted, found) for t in targets if isinstance(t, ast.Name)}
                )
        elif (
            isinstance(node, ast.Attribute)
            and (dotted := literal_module(node.value)) is not None
        ):
            found = module_ok(dotted, node.lineno)
            if found is not None:
                attr_ok(dotted, found, node.attr, node.lineno)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            dotted, found = aliases[node.value.id]
            attr_ok(dotted, found, node.attr, node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in aliases
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            dotted, found = aliases[node.args[0].id]
            attr_ok(dotted, found, node.args[1].value, node.lineno)
    return bad


class HandlerScriptNodeSrc:
    def __init__(self, read: Reader = READ_UTF8) -> None:
        self.read = read

    def handle(self, request: ModelScriptNodeSrcRequest) -> ModelScriptNodeSrcResult:
        tree = ast.parse(self.read(Path(request.script)))
        present = [
            c for c in request.candidates if Path(c, "omniclaude_internal").is_dir()
        ]
        refused = []
        for candidate in present:
            lacking = sorted(
                {what for _, what in unresolved(tree, Path(candidate), self.read)}
            )
            if not lacking:
                return ModelScriptNodeSrcResult(candidate, "")
            refused.append(f"{candidate} lacks {', '.join(lacking)}")
        if not present:
            return ModelScriptNodeSrcResult(
                None,
                "cannot find omniclaude_internal src; set ONEX_REGISTRY_ROOT to the registry root or reinstall the omni plugin",
            )
        return ModelScriptNodeSrcResult(
            None,
            "no node source defines every node name this script takes: "
            + "; ".join(refused)
            + "; set ONEX_REGISTRY_ROOT to a registry root whose plugin repository clone is current or reinstall the omni plugin",
        )
