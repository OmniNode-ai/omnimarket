#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Coverage-json ignore-errors parity gate (OMN-14762 / F-18).

The authoritative coverage gate (``run_coverage_sweep_gate.py``) generates its
census with pytest-cov (``--cov-report=json:...``), which reads coverage's
``[tool.coverage.report]`` config. The shadow aggregator
(``aggregate_coverage_artifacts.py``) generates its census with
``coverage json -i`` (the ``-i`` == ``--ignore-errors`` flag). If those two
diverge on ignore-errors posture, a coverage trace for a C-extension whose
source is not on disk (``dependency_injector/providers.pyx``) makes the
authoritative gate raise ``No source for code`` and go red while the shadow
stays green — the exact F-18 friction (``omnimarket#1794``).

This gate keeps the two invocations aligned, fail-closed:

  1. ``pyproject.toml`` must set ``[tool.coverage.report] ignore_errors = true``
     (or ``[tool.coverage.json] ignore_errors = true``) so the pytest-cov json
     report inherits ignore-errors.
  2. ``aggregate_coverage_artifacts.py`` must invoke ``coverage json`` with the
     ``-i`` / ``--ignore-errors`` flag.

If either drifts, this exits non-zero. SYNC with the pre-commit hook
``coverage-ignore-parity`` and the ci.yml step of the same name.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def report_ignore_errors_set(pyproject: Path) -> bool:
    """True iff pyproject enables ignore-errors for coverage report/json output."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    coverage_cfg = data.get("tool", {}).get("coverage", {})
    for section in ("report", "json"):
        if coverage_cfg.get(section, {}).get("ignore_errors") is True:
            return True
    return False


def aggregator_uses_ignore_flag(aggregate_script: Path) -> bool:
    """True iff the shadow aggregator's ``coverage json`` call carries -i/--ignore-errors.

    Matches the argv-list form used in the script, e.g.
    ``[py, "-m", "coverage", "json", "-i", "-o", ...]`` — the flag must appear
    together with the ``coverage`` + ``json`` tokens so an unrelated ``-i``
    elsewhere cannot spoof the check.
    """
    text = aggregate_script.read_text(encoding="utf-8")
    # Find each "coverage", "json" argv fragment and require an ignore flag near it.
    for match in re.finditer(r'"coverage"\s*,\s*"json"(.*?)\]', text, re.DOTALL):
        frag = match.group(1)
        if '"-i"' in frag or '"--ignore-errors"' in frag:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()

    pyproject = root / "pyproject.toml"
    aggregate_script = root / "scripts" / "ci" / "aggregate_coverage_artifacts.py"

    failures: list[str] = []

    if not pyproject.is_file():
        failures.append(f"pyproject.toml not found at {pyproject}")
    elif not report_ignore_errors_set(pyproject):
        failures.append(
            "pyproject.toml is missing ignore-errors for coverage output: set "
            "[tool.coverage.report] ignore_errors = true (or [tool.coverage.json] "
            "ignore_errors = true). Without it, pytest-cov's json report in "
            "run_coverage_sweep_gate.py raises 'No source for code' on C-extension "
            "traces (F-18) while the shadow aggregator's `coverage json -i` stays "
            "green — the two diverge."
        )

    if not aggregate_script.is_file():
        failures.append(
            f"aggregate_coverage_artifacts.py not found at {aggregate_script}"
        )
    elif not aggregator_uses_ignore_flag(aggregate_script):
        failures.append(
            "aggregate_coverage_artifacts.py no longer invokes `coverage json` with "
            "-i/--ignore-errors. It must keep the ignore-errors flag so its census "
            "matches the authoritative gate's config-driven ignore-errors posture."
        )

    if failures:
        print("coverage-ignore-parity: FAIL", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "coverage-ignore-parity: OK — pyproject enables coverage report/json "
        "ignore_errors and the shadow aggregator uses `coverage json -i`; the "
        "authoritative and shadow coverage-json invocations agree."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
