# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression gate: no hardcoded LAN IPs or absolute user paths in test files.

Scans all .py files under tests/ and flags any line containing:
  - 192.168. (LAN subnet)
  - /Users/  (macOS user home path)
  - /Volumes/ (macOS volume path)

Lines annotated with `# onex-allow-internal-ip` or `# test-literal-ok` are
exempted. File-level annotations (a matching comment anywhere in the file's
first 10 lines) exempt the entire file.

OMN-10579.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"192\.168\."),
    re.compile(r"/Users/"),
    re.compile(r"/Volumes/"),
]

_LINE_EXEMPTION_PATTERN = re.compile(r"#\s*(onex-allow-internal-ip|test-literal-ok)")

_FILE_EXEMPTION_PATTERN = re.compile(r"#\s*(onex-allow-internal-ip|test-literal-ok)")


def _file_is_exempt(lines: list[str]) -> bool:
    """Return True if any of the first 10 lines carries a file-level annotation."""
    return any(_FILE_EXEMPTION_PATTERN.search(line) for line in lines[:10])


def _collect_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(_TESTS_ROOT.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        if _file_is_exempt(lines):
            continue
        rel = str(py_file.relative_to(_TESTS_ROOT))
        for lineno, line in enumerate(lines, start=1):
            if _LINE_EXEMPTION_PATTERN.search(line):
                continue
            for pat in _FORBIDDEN_PATTERNS:
                if pat.search(line):
                    violations.append((rel, lineno, line.strip()))
                    break
    return violations


@pytest.mark.unit
def test_no_hardcoded_literals_in_tests() -> None:
    """All hardcoded LAN IPs and absolute paths must be replaced with fixtures.

    To suppress a specific line: append ``# onex-allow-internal-ip: <reason>``
    or ``# test-literal-ok: <reason>``.

    To suppress an entire file (e.g. a leak-gate test that writes IPs into
    tmp repos): add ``# onex-allow-internal-ip: <reason>`` in the first 10
    lines of the file.
    """
    violations = _collect_violations()
    if not violations:
        return

    lines = ["Hardcoded literals found in test files (use canonical fixtures instead):"]
    for path, lineno, text in violations:
        lines.append(f"  {path}:{lineno}  {text}")
    pytest.fail("\n".join(lines))
