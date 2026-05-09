# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression gate: no hardcoded LAN IPs as os.environ.get defaults in src/.

Scans all .py files under src/ and flags any line matching the pattern:
  os.environ.get("VAR", "http://192.168.x.x:PORT")

or any bare 192.168. string literal used as a default value.

Lines annotated with ``# onex-allow-internal-ip`` are exempted.

OMN-10647.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src"

_LAN_IP_PATTERN = re.compile(r"192\.168\.")
_LINE_EXEMPTION = re.compile(r"#\s*onex-allow-internal-ip")


def _collect_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if py_file.name.startswith("test_"):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(py_file.relative_to(_SRC_ROOT))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LINE_EXEMPTION.search(line):
                continue
            if _LAN_IP_PATTERN.search(line):
                violations.append((rel, lineno, line.strip()))
    return violations


@pytest.mark.unit
def test_no_lan_ip_defaults_in_src() -> None:
    """No hardcoded 192.168.x.x IPs in production source.

    All LAN endpoint env vars must use os.environ["VAR"] (fail-fast) or
    os.environ.get("VAR") with a None default and an explicit guard — never
    a hardcoded IP string as the default value.

    To suppress a specific line: append ``# onex-allow-internal-ip: <reason>``.
    """
    violations = _collect_violations()
    if not violations:
        return

    lines = ["Hardcoded LAN IPs found in src/ (OMN-10647 — use fail-fast env reads):"]
    for path, lineno, text in violations:
        lines.append(f"  {path}:{lineno}  {text}")
    pytest.fail("\n".join(lines))
