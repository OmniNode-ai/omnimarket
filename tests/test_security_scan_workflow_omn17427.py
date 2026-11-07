# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard the security-scan Safety gate (OMN-17427).

The scanner must be pinned, and the enforcing invocation must stay enforcing:
only an exit-1 crash may be retried, never a finding (exit 64), and the step
must not end in a bare ``|| true``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "security-scan.yml"


def _safety_script() -> str:
    data = yaml.safe_load(WORKFLOW.read_text())
    steps = data["jobs"]["dependency-scan"]["steps"]
    (step,) = [s for s in steps if s.get("name") == "Run Safety Check"]
    return str(step["run"])


def test_safety_install_is_pinned() -> None:
    installs = re.findall(r"pip install\s+(\S+)", _safety_script())
    assert installs, "Run Safety Check must install safety"
    for spec in installs:
        assert re.fullmatch(r"safety==\d+\.\d+\.\d+", spec), spec


def test_enforcing_invocation_has_no_or_true() -> None:
    plain = [
        line.strip()
        for line in _safety_script().splitlines()
        if line.strip().startswith("safety check") and "--output json" not in line
    ]
    assert plain, "enforcing safety invocation missing"
    for line in plain:
        assert "|| true" not in line, line


def test_only_crash_exit_is_retried() -> None:
    script = _safety_script()
    assert '[ "$rc" -ne 1 ]' in script  # any other code (0, 64 finding) exits now
    assert script.rstrip().endswith("exit 1")  # persistent crash still fails
