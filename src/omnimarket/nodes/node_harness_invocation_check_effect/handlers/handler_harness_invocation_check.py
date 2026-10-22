# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a harness invocation outside the delegation effect node's handlers.

A harness is a coding-agent CLI (codex, claude) or a hosted model endpoint
(OpenRouter, GLM). Every invocation belongs in a handler of
node_llm_delegation_call_effect. This handler reads Python and shell sources,
finds each place one starts a harness CLI or calls a harness HTTP endpoint, and
refuses every site outside that directory that the contract's shrink-only
baseline does not hold. A baseline entry whose sites are gone is refused too,
so the list can only shrink.
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

import yaml

from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    EnumHarnessInvocationKind,
    ModelHarnessInvocationBaselineEntry,
    ModelHarnessInvocationCheckRequest,
    ModelHarnessInvocationCheckResult,
    ModelHarnessInvocationPolicy,
    ModelHarnessInvocationSite,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# Process-spawning callables and the receivers they are reached through.
_SPAWN_FUNCS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "system",
        "popen",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "spawnl",
        "spawnlp",
        "posix_spawn",
        "posix_spawnp",
        "spawn",
    }
)
_SPAWN_MODULES = frozenset({"subprocess", "asyncio", "os", "pty"})
# Callables whose url, base_url or positional arguments reach an HTTP endpoint.
_HTTP_FUNCS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "request",
        "stream",
        "send",
        "urlopen",
        "Request",
        "Client",
        "AsyncClient",
        "OpenAI",
        "AsyncOpenAI",
        "ClientSession",
    }
)
_SHELL_HTTP = re.compile(r"\b(curl|wget|http|https)\b")


def load_policy(contract_path: Path = CONTRACT_PATH) -> ModelHarnessInvocationPolicy:
    """Read harness_invocation_policy from the node contract."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelHarnessInvocationPolicy.model_validate(
        contract["harness_invocation_policy"]
    )


def _program(value: object, targets: frozenset[str]) -> str | None:
    """Return the harness a program word names, by its basename."""
    if not isinstance(value, str):
        return None
    words = value.strip().split()
    if not words:
        return None
    name = PurePosixPath(words[0]).name
    return name if name in targets else None


def _constant(node: ast.AST | None) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _constant(node.values[0])
    return None


class _PythonScanner:
    def __init__(self, tree: ast.Module, policy: ModelHarnessInvocationPolicy) -> None:
        self._tree = tree
        self._policy = policy
        self._targets = frozenset(policy.cli_targets)
        self._hosts = policy.http_hosts
        self._spawn_names = self._imported_spawn_names(tree)
        self._assigned: dict[str, list[ast.expr]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._assigned.setdefault(target.id, []).append(node.value)
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and isinstance(node.target, ast.Name)
            ):
                self._assigned.setdefault(node.target.id, []).append(node.value)

    @staticmethod
    def _imported_spawn_names(tree: ast.Module) -> frozenset[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _SPAWN_MODULES:
                for alias in node.names:
                    if alias.name in _SPAWN_FUNCS:
                        names.add(alias.asname or alias.name)
        return frozenset(names)

    def _is_spawn(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute):
            return (
                func.attr in _SPAWN_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id in _SPAWN_MODULES
            )
        return isinstance(func, ast.Name) and func.id in self._spawn_names

    def _argv_program(self, node: ast.expr | None, depth: int = 0) -> str | None:
        """Resolve the harness an argv or shell-string expression starts."""
        if node is None or depth > 4:
            return None
        if isinstance(node, ast.List | ast.Tuple):
            if not node.elts:
                return None
            return self._argv_program(node.elts[0], depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._argv_program(node.left, depth + 1)
        if isinstance(node, ast.Name):
            for value in self._assigned.get(node.id, []):
                found = self._argv_program(value, depth + 1)
                if found is not None:
                    return found
            return None
        if isinstance(node, ast.Starred):
            return self._argv_program(node.value, depth + 1)
        return _program(_constant(node), self._targets)

    def _host_in(self, node: ast.AST, depth: int = 0) -> str | None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for host in self._hosts:
                    if host in sub.value:
                        return host
            elif isinstance(sub, ast.Name) and depth < 4:
                for value in self._assigned.get(sub.id, []):
                    found = self._host_in(value, depth + 1)
                    if found is not None:
                        return found
        return None

    def _docstrings(self) -> set[int]:
        ids: set[int] = set()
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                ids.add(id(node.value))
        return ids

    def embedded_sites(self) -> Iterator[tuple[int, EnumHarnessInvocationKind, str]]:
        """Harness command lines inside string literals, such as a remote script."""
        command = _command_pattern(self._policy, embedded=True)
        docstrings = self._docstrings()
        for node in ast.walk(self._tree):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or id(node) in docstrings
            ):
                continue
            for offset, _line, program in _shell_lines(node.value, command):
                if program is not None:
                    yield (
                        node.lineno + offset - 1,
                        EnumHarnessInvocationKind.CLI_EXEC,
                        program,
                    )

    def sites(self) -> Iterator[tuple[int, EnumHarnessInvocationKind, str]]:
        yield from self.embedded_sites()
        for node in ast.walk(self._tree):
            if not isinstance(node, ast.Call):
                continue
            if self._is_spawn(node.func):
                argv = node.args[0] if node.args else None
                for keyword in node.keywords:
                    if keyword.arg in {"args", "cmd", "file", "path", "program"}:
                        argv = argv or keyword.value
                program = self._argv_program(argv)
                if program is not None:
                    yield node.lineno, EnumHarnessInvocationKind.CLI_EXEC, program
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else None
            )
            if name not in _HTTP_FUNCS:
                continue
            reached: list[ast.AST] = list(node.args)
            reached += [
                k.value
                for k in node.keywords
                if k.arg in {"url", "base_url", "endpoint", "endpoint_url"}
            ]
            for arg in reached:
                host = self._host_in(arg)
                if host is not None:
                    yield node.lineno, EnumHarnessInvocationKind.HTTP, host
                    break


def _python_sites(
    source: str, policy: ModelHarnessInvocationPolicy
) -> Iterator[tuple[int, EnumHarnessInvocationKind, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    yield from _PythonScanner(tree, policy).sites()


def _command_pattern(
    policy: ModelHarnessInvocationPolicy, *, embedded: bool
) -> re.Pattern[str]:
    """A harness name in command position: line start, after a separator or a prefix.

    In a Python string a backtick quotes prose rather than substituting a
    command, and the name must be followed by a flag or codex's exec verb, so
    prose such as "claude limited until" is not a command line.
    """
    names = "|".join(re.escape(t) for t in policy.cli_targets)
    separators = r"[;&|({]" if embedded else r"[;&|(`{]"
    tail = r"(?=\s+(?:-|exec\b))" if embedded else r"(?=\s|$|;|\)|&|\|)"
    return re.compile(
        rf"(?:^|{separators}|\$\(|\b(?:exec|then|do|else|nohup|command|time|xargs|env)\b"
        r"|\btimeout\s+\S+|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?(?=\s))\s*"
        r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:\S*/)?"
        rf"({names}){tail}"
    )


def _shell_lines(
    source: str, command: re.Pattern[str]
) -> Iterator[tuple[int, str, str | None]]:
    """Each non-comment line with the harness it starts in command position."""
    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"\s#.*$", "", line)
        match = command.search(line)
        yield lineno, line, match.group(1) if match is not None else None


def _shell_sites(
    source: str, policy: ModelHarnessInvocationPolicy
) -> Iterator[tuple[int, EnumHarnessInvocationKind, str]]:
    command = _command_pattern(policy, embedded=False)
    for lineno, line, program in _shell_lines(source, command):
        if program is not None:
            yield lineno, EnumHarnessInvocationKind.CLI_EXEC, program
            continue
        if _SHELL_HTTP.search(line):
            for host in policy.http_hosts:
                if host in line:
                    yield lineno, EnumHarnessInvocationKind.HTTP, host
                    break


def scan_source(
    rel_path: str, source: str, policy: ModelHarnessInvocationPolicy
) -> list[ModelHarnessInvocationSite]:
    """Every harness invocation site in one file's text."""
    finder = _python_sites if rel_path.endswith(".py") else _shell_sites
    return [
        ModelHarnessInvocationSite(path=rel_path, line=line, kind=kind, target=target)
        for line, kind, target in finder(source, policy)
    ]


def _repo_files(root: Path, policy: ModelHarnessInvocationPolicy) -> Iterator[Path]:
    excluded = set(policy.excluded_dir_names)
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        for name in sorted(filenames):
            if os.path.splitext(name)[1] in policy.scan_suffixes:
                yield Path(directory) / name


def _sanctioned(rel_path: str, policy: ModelHarnessInvocationPolicy) -> bool:
    return rel_path.startswith(policy.sanctioned_handler_dir.rstrip("/") + "/")


def _key(
    item: ModelHarnessInvocationSite | ModelHarnessInvocationBaselineEntry,
) -> tuple[str, str, str]:
    return item.path, item.kind.value, item.target


def judge(
    sites: Iterable[ModelHarnessInvocationSite],
    policy: ModelHarnessInvocationPolicy,
    *,
    full_scan: bool,
) -> ModelHarnessInvocationCheckResult:
    """Refuse unsanctioned sites beyond the baseline; on a full scan, stale entries."""
    found = tuple(sites)
    outside = [s for s in found if not _sanctioned(s.path, policy)]
    allowance = Counter({_key(e): e.count for e in policy.baseline})
    seen: Counter[tuple[str, str, str]] = Counter()
    refused: list[ModelHarnessInvocationSite] = []
    for site in outside:
        seen[_key(site)] += 1
        if seen[_key(site)] > allowance[_key(site)]:
            refused.append(site)
    stale = (
        tuple(e for e in policy.baseline if seen[_key(e)] < e.count)
        if full_scan
        else ()
    )
    lines: list[str] = []
    if refused:
        lines.append(
            f"{len(refused)} harness invocation(s) outside "
            f"{policy.sanctioned_handler_dir}:"
        )
        lines += [f"  {s.path}:{s.line}: {s.kind.value} {s.target}" for s in refused]
        lines.append(
            "Fix: invoke the harness through node_llm_delegation_call_effect; "
            "the baseline only shrinks."
        )
    if stale:
        lines.append(
            f"{len(stale)} baseline entr(y/ies) now cover fewer sites; "
            "lower count or delete:"
        )
        lines += [
            f"  {e.path}: {e.kind.value} {e.target} count={e.count} "
            f"found={seen[_key(e)]}"
            for e in stale
        ]
    admitted = not refused and not stale
    if admitted:
        lines.append(
            f"OK: {len(found)} harness invocation site(s), "
            f"{len(outside)} held by the shrink-only baseline."
        )
    return ModelHarnessInvocationCheckResult(
        admitted=admitted,
        exit_code=0 if admitted else 1,
        sites=found,
        refused=tuple(refused),
        stale_baseline=stale,
        output_lines=tuple(lines),
    )


class HandlerHarnessInvocationCheck:
    """EFFECT handler: read a repository's sources and judge harness invocations."""

    def __init__(self, policy: ModelHarnessInvocationPolicy | None = None) -> None:
        self._policy = policy or load_policy()

    def handle(
        self, request: ModelHarnessInvocationCheckRequest
    ) -> ModelHarnessInvocationCheckResult:
        root = Path(request.repo_root).resolve()
        if request.paths:
            files = [(root / p).resolve() for p in request.paths]
        else:
            files = list(_repo_files(root, self._policy))
        sites: list[ModelHarnessInvocationSite] = []
        for path in files:
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root).as_posix()
            sites += scan_source(rel, source, self._policy)
        return judge(sites, self._policy, full_scan=not request.paths)
