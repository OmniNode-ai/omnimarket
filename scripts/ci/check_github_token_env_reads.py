#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Enforcement gate: raw GitHub-token env reads in omnimarket source (OMN-12856).

Flags any ``os.environ["GH_PAT"]``, ``os.environ.get("GH_PAT")``,
``os.getenv("GH_PAT")``, or any equivalent access to ``GITHUB_TOKEN`` /
``GH_TOKEN`` / ``GH_PAT`` that appears outside the test tree. After OMN-12856
the canonical pattern is:

    ref = contract_secret_ref(CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref)

The secret-store resolver (``omnimarket.inference.secret_store_resolver``) IS
the only allowed effect-boundary resolution. Raw env subscripts in handler /
adapter / consumer source are always violations.

Scope:
    ``src/omnimarket/`` Python files, excluding ``tests/``, ``conftest.py``,
    and inline-annotated lines (``# omn-allow-env-read`` or ``# ONEX_EXCLUDE``).

Exit codes:
    0 — scan completed, no violations (or report mode with violations).
    1 — violations found (enforce mode only, default).

Usage:
    uv run python scripts/ci/check_github_token_env_reads.py           # enforce
    uv run python scripts/ci/check_github_token_env_reads.py --report  # warn only
    uv run python scripts/ci/check_github_token_env_reads.py --verbose
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Secret-name literals that are GitHub-token env reads after OMN-12856.
# Any os.environ / os.getenv access to these names in production source is a
# violation — the value must come from resolve_api_key(contract_secret_ref(...)).
_GITHUB_TOKEN_ENV_NAMES: frozenset[str] = frozenset(
    {"GH_PAT", "GH_TOKEN", "GITHUB_TOKEN"}
)

# Inline skip annotations accepted on the read line or any line of a multi-line call.
_SKIP_ANNOTATIONS: tuple[str, ...] = ("# omn-allow-env-read", "# ONEX_EXCLUDE")

_ALLOWLISTED_PATH_SEGMENTS: tuple[str, ...] = (
    "tests/",
    "conftest.py",
    "__pycache__/",
    ".pyc",
)


def _is_allowlisted(rel_path: str) -> bool:
    return any(seg in rel_path for seg in _ALLOWLISTED_PATH_SEGMENTS)


def _arg_name(node: ast.expr) -> str:
    """Best-effort string repr of an env-lookup key."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _has_skip(lines: list[str], start: int, end: int) -> bool:
    """True if any line in [start-1, end] (0-based) carries a skip annotation."""
    for ln in lines[max(0, start - 1) : end]:
        if any(ann in ln for ann in _SKIP_ANNOTATIONS):
            return True
    return False


def _scan_file(path: Path) -> list[str]:
    """Return violation strings for raw GitHub-token env reads in *path*."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    violations: list[str] = []

    for node in ast.walk(tree):
        lineno: int | None = None
        end_lineno: int = 0
        arg_name: str = ""
        detail: str = ""

        # os.environ["GH_PAT"] — Subscript
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        ):
            key = _arg_name(node.slice)
            if key in _GITHUB_TOKEN_ENV_NAMES:
                lineno = node.lineno
                end_lineno = getattr(node, "end_lineno", node.lineno)
                arg_name = key
                detail = f"os.environ[{key!r}]"

        elif isinstance(node, ast.Call):
            func = node.func
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            )
            if (is_getenv or is_environ_get) and node.args:
                key = _arg_name(node.args[0])
                if key in _GITHUB_TOKEN_ENV_NAMES:
                    lineno = node.lineno
                    end_lineno = getattr(node, "end_lineno", node.lineno)
                    arg_name = key
                    call_form = "os.getenv" if is_getenv else "os.environ.get"
                    detail = f"{call_form}({key!r})"

        if lineno is not None and not _has_skip(lines, lineno, end_lineno):
            line_text = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            violations.append(
                f"{path}:{lineno}: raw GitHub-token env read {detail!r} — "
                f"use contract_secret_ref + resolve_api_key instead. "
                f"({line_text!r})"
            )

    return violations


def _find_repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    return Path.cwd()


def scan(repo_root: Path | None = None) -> list[str]:
    """Return all violations found under ``src/omnimarket/``."""
    if repo_root is None:
        repo_root = _find_repo_root()

    src_root = repo_root / "src" / "omnimarket"
    if not src_root.exists():
        return []

    all_violations: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        rel = str(py_file.relative_to(repo_root)).replace("\\", "/")
        if _is_allowlisted(rel):
            continue
        all_violations.extend(_scan_file(py_file))

    return all_violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce: no raw GitHub-token env reads in omnimarket source (OMN-12856)."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Report violations but always exit 0 (warn-only mode).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    repo_root = _find_repo_root()
    violations = scan(repo_root)

    if violations:
        mode = "WARN" if args.report else "FAIL"
        print(
            f"[github-token-env-gate] [{mode}] "
            f"{len(violations)} raw GitHub-token env read(s) found:"
        )
        for v in violations:
            print(f"  {v}")
        if not args.report:
            print(
                "\nFix: replace os.environ/os.getenv with:\n"
                "    from omnimarket.nodes.contract_topics import contract_secret_ref\n"
                "    from omnimarket.inference.secret_store_resolver import resolve_api_key\n"
                "    ref = contract_secret_ref(CONTRACT_PATH, 'GITHUB_TOKEN')\n"
                "    secret = resolve_api_key(ref)\n"
                "(OMN-12856)"
            )
            return 1
    elif args.verbose:
        py_count = sum(
            1
            for _ in (repo_root / "src" / "omnimarket").rglob("*.py")
            if not _is_allowlisted(str(_))
        )
        print(f"[github-token-env-gate] OK — scanned {py_count} files, no violations.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
