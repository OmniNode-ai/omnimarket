#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Validate that no silent localhost/hardcoded-endpoint fallbacks exist in src/.

Scans src/**/*.py for patterns like:
  - os.environ.get("KEY", "localhost...")
  - os.getenv("KEY", "localhost...")
  - os.getenv("KEY", "bolt://localhost...")
  - os.getenv("KEY", "http://localhost...")
  - default="bolt://localhost..."
  - default="http://localhost..."

Skips docstrings and comments. Exits non-zero if any violations are found.

Usage:
    uv run python scripts/validation/validate_no_env_fallbacks.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"

FALLBACK_PATTERNS = [
    re.compile(
        r'os\.environ\.get\([^)]*,\s*["\'].*(?:localhost|bolt://|http://localhost|redis://localhost|postgresql://localhost)'
    ),
    re.compile(
        r'os\.getenv\([^)]*,\s*["\'].*(?:localhost|bolt://|http://localhost|redis://localhost|postgresql://localhost)'
    ),
    re.compile(
        r'default\s*=\s*["\'](?:bolt://localhost|http://localhost|redis://localhost|postgresql://localhost)'
    ),
]

SKIP_DIRS = {"tests", "__tests__", "test", "__pycache__"}


def scan_file(filepath: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return violations

    in_docstring = False
    docstring_delim: str | None = None

    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()

        # Track triple-quoted docstring boundaries
        for delim in ('"""', "'''"):
            count = stripped.count(delim)
            if in_docstring and docstring_delim == delim and count >= 1:
                in_docstring = False
                docstring_delim = None
                break
            if not in_docstring and count == 1:
                in_docstring = True
                docstring_delim = delim
                break
            if count >= 2:
                break  # opens and closes on the same line — not a block docstring

        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue

        for pattern in FALLBACK_PATTERNS:
            if pattern.search(line):
                violations.append((lineno, stripped))
                break

    return violations


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f"ERROR: src directory not found at {SRC_DIR}", file=sys.stderr)
        return 1

    all_violations: list[tuple[str, int, str]] = []

    for py_file in sorted(SRC_DIR.rglob("*.py")):
        if any(part in SKIP_DIRS for part in py_file.parts):
            continue
        for lineno, line in scan_file(py_file):
            all_violations.append((str(py_file.relative_to(REPO_ROOT)), lineno, line))

    if all_violations:
        print(f"FAIL: {len(all_violations)} silent env-fallback violation(s) found:\n")
        for filepath, lineno, line in all_violations:
            print(f"  {filepath}:{lineno}: {line}")
        print(
            "\nReplace os.getenv/os.environ.get with localhost fallbacks with "
            "os.environ[KEY] (fail-fast) or remove the hardcoded default."
        )
        return 1

    print("OK: No silent env-fallback violations found in src/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
