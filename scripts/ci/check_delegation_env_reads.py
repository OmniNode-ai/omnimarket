#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CI scanner for os.environ/os.getenv reads in delegation-owned modules.

Detects direct environment variable access in delegation modules. Report mode
(warn only) is used in Wave 0. Enforce mode (exit 1) is reserved for Wave 2
after contract-driven config is landed.

Module scope: nodes matching delegation/LLM-inference/bifrost patterns.

Exit codes:
    0: Scan completed (report mode always returns 0)
    1: Violations found (enforce mode only)

Usage:
    python scripts/ci/check_delegation_env_reads.py
    python scripts/ci/check_delegation_env_reads.py --mode enforce
    python scripts/ci/check_delegation_env_reads.py --verbose
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

DELEGATION_MODULE_PATTERNS = [
    "nodes/node_delegation_",
    "nodes/node_delegate_skill_",
    "nodes/node_delegation_routing_",
    "nodes/node_llm_inference_",
    "nodes/node_llm_completion_",
    "nodes/node_llm_embedding_",
    "nodes/node_delegation_quality_gate_",
    "nodes/node_projection_delegation",
    "delegation/",
    "adapters/llm/",
]

ALLOWLISTED_PATH_SEGMENTS = [
    "tests/",
    "fixtures/",
    "conftest.py",
    "__pycache__/",
]

# Inline skip token: # ONEX_FLAG_EXEMPT or # ONEX_EXCLUDE allows a line
SKIP_TOKENS = ["ONEX_FLAG_EXEMPT", "ONEX_EXCLUDE"]


@dataclass
class ScanResult:
    scanned_files: int = 0
    violations: list[str] = field(default_factory=list)
    report_generated: bool = False


def _is_allowlisted(rel_path: str) -> bool:
    return any(segment in rel_path for segment in ALLOWLISTED_PATH_SEGMENTS)


def _is_delegation_module(rel_path: str) -> bool:
    return any(pattern in rel_path for pattern in DELEGATION_MODULE_PATTERNS)


def _has_skip_token(line: str) -> bool:
    return any(token in line for token in SKIP_TOKENS)


def _find_env_calls_in_source(source: str, filepath: str) -> list[str]:
    """Return list of violation strings for env calls in the given source.

    Deduplicates by line number, keeping the most specific match
    (os.environ.get > os.environ > os.getenv).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()

    # Map lineno -> (call_repr, specificity, end_lineno) — higher specificity wins
    # Walk ast.Call (not ast.Attribute) so end_lineno covers the closing ) and any
    # inline ONEX_EXCLUDE comment on the same line as the closing paren.
    # specificity: os.environ.get=2, os.environ[...]=1, os.getenv=1
    best_per_line: dict[int, tuple[str, int, int]] = {}

    for node in ast.walk(tree):
        lineno: int | None = None
        call_repr: str | None = None
        specificity: int = 1
        end_lineno: int

        # os.getenv(...) or os.environ[...] (Subscript, not a Call)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in ("environ", "getenv")
        ):
            lineno = node.lineno
            call_repr = f"os.{node.attr}"
            end_lineno = getattr(node, "end_lineno", node.lineno)

        # os.environ.get(...) or os.getenv(...) — prefer Call for accurate end_lineno
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                lineno = node.lineno
                call_repr = "os.getenv"
                end_lineno = getattr(node, "end_lineno", node.lineno)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            ):
                lineno = node.lineno
                call_repr = "os.environ.get"
                specificity = 2
                end_lineno = getattr(node, "end_lineno", node.lineno)

        if lineno is not None and call_repr is not None:
            existing = best_per_line.get(lineno)
            if existing is None or specificity > existing[1]:
                best_per_line[lineno] = (call_repr, specificity, end_lineno)

    violations: list[str] = []
    for lineno in sorted(best_per_line):
        call_repr, _, end_lineno = best_per_line[lineno]
        line_text = lines[lineno - 1] if lineno <= len(lines) else ""
        # Check all lines of a multi-line call for skip tokens (handles formatter-wrapped calls)
        call_lines = (
            lines[lineno - 1 : end_lineno] if end_lineno > lineno else [line_text]
        )
        if any(_has_skip_token(ln) for ln in call_lines):
            continue
        violations.append(f"{filepath}:{lineno}: {call_repr} — {line_text.strip()}")

    return violations


def scan_delegation_modules(
    repo_root: Path | None = None,
    mode: str = "report",
) -> ScanResult:
    if repo_root is None:
        # Walk up from this file to find the repo root (.git dir)
        candidate = Path(__file__).parent
        while candidate != candidate.parent:
            if (candidate / ".git").exists():
                repo_root = candidate
                break
            candidate = candidate.parent
        else:
            repo_root = Path.cwd()

    result = ScanResult()

    src_root = repo_root / "src"
    if not src_root.exists():
        result.report_generated = True
        return result

    for py_file in src_root.rglob("*.py"):
        rel = str(py_file.relative_to(repo_root))
        rel_forward = rel.replace("\\", "/")

        if _is_allowlisted(rel_forward):
            continue
        if not _is_delegation_module(rel_forward):
            continue

        result.scanned_files += 1
        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        violations = _find_env_calls_in_source(source, rel_forward)
        result.violations.extend(violations)

    result.report_generated = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan delegation modules for direct os.environ/os.getenv usage"
    )
    parser.add_argument(
        "--mode",
        choices=["report", "enforce"],
        default="report",
        help="report: warn only (default). enforce: exit 1 on violations.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Locate repo root from CWD
    candidate = Path.cwd()
    repo_root: Path = candidate
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            repo_root = candidate
            break
        candidate = candidate.parent

    result = scan_delegation_modules(repo_root=repo_root, mode=args.mode)

    if result.violations:
        print(
            f"[delegation-env-scanner] Found {len(result.violations)} env read(s) "
            f"in delegation modules ({args.mode} mode)"
        )
        for v in sorted(result.violations):
            print(f"  {v}")
        if args.mode == "enforce":
            print(
                "\nFix: replace os.environ/os.getenv with contract-driven config "
                "resolution (OMN-10915 Wave 2)."
            )
            return 1
        print(
            "\n[report mode] These will become blocking violations in Wave 2 "
            "(OMN-10917 → OMN-10915)."
        )
    elif args.verbose:
        print(
            f"[delegation-env-scanner] OK — scanned {result.scanned_files} files, "
            "no violations."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
