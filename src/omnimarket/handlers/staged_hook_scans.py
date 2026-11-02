# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Staged-diff entry points of four commit hooks, and a faster fifth (OMN-22322).

Four commit hooks used to read every file of the repository on every commit:
``retired-model-references``, ``harness-invocation-check``,
``rls-read-tenant-seam`` and ``deployment-fact-gate``. Each rule below is
local to one file's content (plus, for two of them, a small global input named
below), so the hook judges the files the commit changes and reaches the verdict
the whole-tree scan reaches for the same commit. The whole-tree mode of every
check is unchanged: it is what CI runs and what a call with no file arguments
(``pre-commit run --all-files`` passes every file, a bare ``python -m`` call
passes none) does.

    uv run --frozen python -m omnimarket.handlers.staged_hook_scans <scan> [path ...]

``scan`` is one of ``retired-model-references``, ``harness-invocation-check``,
``rls-read-tenant-seam`` and ``deployment-fact-gate``, or ``handler-dispatch-entrypoint``,
which stays a whole-tree scan (a handler can inherit its entrypoint from a module the
commit does not touch) and only parses YAML with libyaml. The first three take the
staged file names pre-commit passes; ``deployment-fact-gate`` reads the changed
files from git itself.

Why each scan's staged verdict equals its whole-tree verdict:

* retired-model-references: a finding is a regular-expression match in one
  file's name or bytes. No finding depends on a second file.
* harness-invocation-check: a refused site is a site beyond its
  ``(path, kind, target)`` allowance, and both the sites and the allowance are
  keyed on the file's own path. A stale baseline entry is one whose path now
  holds fewer sites than it counts, so it is judged for the entries whose path
  is a staged file or no longer exists. The baseline lives in the node
  contract, which the hook's ``files`` filter does not select, so a baseline
  edit alone never reached this hook.
* rls-read-tenant-seam: a finding depends on one module and on the set of
  relations the migrations put under row level security. That set is read in
  full on every run, and a staged ``.sql`` file (a migration) falls back to the
  whole tree.
* deployment-fact-gate: a new routing comparison is a key whose count in the
  tree is above its count at the base revision, and a key names its file. A
  file whose text equals its text at the base revision has equal counts for
  every key, so only the files that differ from the base can carry a new key.
  The packaged-config judgement is unchanged and still reads all three files.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Final

import yaml

from omnimarket.nodes.node_deployment_fact_gate_effect.__main__ import _render
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    DeploymentFactGateError,
    judge_packaged_configs,
    read_packaged_config_texts,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_routing_in_code import (
    SCANNED_SOURCE_DIR,
    RoutingInCodeGateError,
    _is_scanned,
    judge_routing_branches,
    routing_branches,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models.model_deployment_fact_gate import (
    ModelDeploymentFactGateRequest,
)
from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
    judge,
    load_policy,
    scan_source,
)
from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    ModelHarnessInvocationCheckRequest,
    ModelHarnessInvocationSite,
)
from omnimarket.nodes.node_retired_model_reference_check_effect.handlers.handler_retired_model_reference_check import (
    HandlerRetiredModelReferenceCheck,
)
from omnimarket.nodes.node_retired_model_reference_check_effect.models.model_retired_model_reference_check import (
    ModelRetiredModelReferenceCheckRequest,
)

_RLS_SCRIPT: Final[str] = "scripts/ci/check_rls_read_tenant_seam.py"
_GIT_TIMEOUT_S: Final[int] = 60


def retired_model_references(root: Path, paths: Sequence[str]) -> int:
    """Refuse a retired-model reference in the named files (every file when none)."""
    result = HandlerRetiredModelReferenceCheck().handle(
        ModelRetiredModelReferenceCheckRequest(repo_root=str(root), paths=tuple(paths))
    )
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


def harness_invocation_check(root: Path, paths: Sequence[str]) -> int:
    """Judge harness invocations in the named files; every file when none."""
    if not paths:
        result = HandlerHarnessInvocationCheck().handle(
            ModelHarnessInvocationCheckRequest(repo_root=str(root))
        )
    else:
        policy = load_policy()
        excluded = set(policy.excluded_dir_names)
        scanned: dict[str, Path] = {}
        for name in paths:
            path = (root / name).resolve()
            rel = path.relative_to(root).as_posix()
            if path.suffix not in policy.scan_suffixes:
                continue
            if excluded & set(PurePosixPath(rel).parts[:-1]):
                continue
            scanned[rel] = path
        sites: list[ModelHarnessInvocationSite] = []
        for rel, path in sorted(scanned.items()):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sites += scan_source(rel, source, policy)
        judged = policy.model_copy(
            update={
                "baseline": tuple(
                    entry
                    for entry in policy.baseline
                    if entry.path in scanned or not (root / entry.path).exists()
                )
            }
        )
        result = judge(sites, judged, full_scan=True)
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


def _load_rls_script(root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_rls_read_tenant_seam", root / _RLS_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {root / _RLS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rls_read_tenant_seam(root: Path, paths: Sequence[str]) -> int:
    """Run the RLS tenant-seam gate over the named modules; the tree when none or a migration."""
    gate = _load_rls_script(root)
    scan_root = Path("src/omnimarket")
    named = [(root / name).resolve() for name in paths]
    migration_staged = any(p.suffix == ".sql" for p in named)
    if paths and not migration_staged:
        modules = sorted(
            scan_root / p.relative_to(root / scan_root)
            for p in named
            if p.suffix == ".py" and p.is_relative_to(root / scan_root)
        )

        scan_tree = gate.scan

        def scan_named(tree: Path) -> list[object]:
            relations = gate.rls_relations(tree)
            if not relations:
                return list(scan_tree(tree))  # prints the fail-closed message and exits
            findings: list[object] = []
            for py_path in modules:
                if not py_path.is_file():
                    continue
                try:
                    parsed = ast.parse(py_path.read_text(errors="ignore"))
                except SyntaxError:
                    continue
                facts = vars(gate)["_scan_module"](parsed, relations)
                if not (facts.drivers and facts.statement_lines):
                    continue
                if not facts.relation_hits:
                    continue
                if facts.seam_guc and facts.seam_resolver:
                    continue
                findings.append(
                    gate.Finding(
                        path=py_path,
                        line=min(facts.relation_hits.values()),
                        relations=tuple(sorted(facts.relation_hits)),
                    )
                )
            return findings

        vars(gate)["scan"] = (
            scan_named  # the gate's own main() prints and counts the findings
        )
    return int(gate.main(["--root", str(scan_root)]))


def _git_z(root: Path, *args: str) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise DeploymentFactGateError(
            f"git {' '.join(args)} failed in {root}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()}"
        )
    return [name.decode() for name in done.stdout.split(b"\0") if name]


def _git_text(root: Path, base_ref: str, relative: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(root), "show", f"{base_ref}:{relative}"],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    return done.stdout.decode("utf-8") if done.returncode == 0 else None


def changed_scanned_files(root: Path, base_ref: str) -> list[str]:
    """Scanned source files whose text differs from ``base_ref``, or that are untracked."""
    differing = _git_z(
        root,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        base_ref,
        "--",
        SCANNED_SOURCE_DIR,
    )
    untracked = _git_z(
        root,
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
        "--",
        SCANNED_SOURCE_DIR,
    )
    return sorted(
        {
            name
            for name in (*differing, *untracked)
            if _is_scanned(PurePosixPath(name)) and (root / name).is_file()
        }
    )


def deployment_fact_gate(root: Path, base_ref: str = "HEAD") -> int:
    """The deployment-fact gate against ``base_ref``, parsing only files that changed."""
    request = ModelDeploymentFactGateRequest(repo_root=str(root), base_ref=base_ref)
    try:
        texts = read_packaged_config_texts(root, request.base_ref)
        configs = judge_packaged_configs(texts, request.base_ref)
        changed = changed_scanned_files(root, base_ref)
        head = routing_branches(
            (name, (root / name).read_text(encoding="utf-8")) for name in changed
        )
        base = routing_branches(
            (name, text)
            for name in changed
            if (text := _git_text(root, base_ref, name)) is not None
        )
    except (DeploymentFactGateError, RoutingInCodeGateError) as exc:
        sys.stderr.write(f"deployment-fact gate cannot judge: {exc}\n")
        return 2
    result = configs.model_copy(
        update={"new_routing_branches": judge_routing_branches(head, base)}
    )
    report = _render(result, False)
    if report:
        (sys.stdout if result.passed else sys.stderr).write(report + "\n")
    return 0 if result.passed else 1


@contextlib.contextmanager
def libyaml_parser() -> Iterator[None]:
    """Parse YAML with libyaml for the length of the block (PyYAML's own parser without it)."""
    loader = getattr(yaml, "CSafeLoader", None)
    original = yaml.safe_load
    if loader is not None:
        vars(yaml)["safe_load"] = lambda stream: yaml.load(stream, Loader=loader)
    try:
        yield
    finally:
        vars(yaml)["safe_load"] = original


def handler_dispatch_entrypoint(argv: Sequence[str]) -> int:
    """The dispatch-entrypoint gate over the whole tree, reading YAML with libyaml.

    The gate imports every contract-declared handler module and many of those modules
    read their own contract at import; PyYAML's pure-Python parser was about 40 percent
    of the 19 s. Which handlers are judged, and the verdict, are the gate's own.
    """
    from omnimarket.validators import (
        handler_dispatch_entrypoint as gate,
    )

    with libyaml_parser():
        return int(gate.main(list(argv) or [str(gate.DEFAULT_SCAN_ROOT)]))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(
            "usage: staged_hook_scans <retired-model-references|"
            "harness-invocation-check|rls-read-tenant-seam|deployment-fact-gate|"
            "handler-dispatch-entrypoint> [path ...]\n"
        )
        return 64
    scan, paths = args[0], args[1:]
    root = Path.cwd().resolve()
    if scan == "retired-model-references":
        return retired_model_references(root, paths)
    if scan == "harness-invocation-check":
        return harness_invocation_check(root, paths)
    if scan == "rls-read-tenant-seam":
        return rls_read_tenant_seam(root, paths)
    if scan == "deployment-fact-gate":
        return deployment_fact_gate(root)
    if scan == "handler-dispatch-entrypoint":
        return handler_dispatch_entrypoint(paths)
    sys.stderr.write(f"unknown scan {scan!r}\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
