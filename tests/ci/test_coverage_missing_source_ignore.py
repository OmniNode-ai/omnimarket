# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: coverage-json generation must ignore missing-source records (OMN-14762 / F-18).

F-18: ``omnimarket#1794`` Coverage Aggregate failed on
``No source for code: .../dependency_injector/providers.pyx`` — a Cython
C-extension that ships only as a compiled ``.so``, so its ``.pyx`` source is not
on disk in the checkout, yet coverage's tracer recorded a trace for it. Without
ignore-errors, ``coverage json`` raises and the gate goes red.

MANDATORY RED PROOF: the test first proves the failure is REAL against the exact
mechanism (a coverage dataset containing a record whose source file does not
exist) — the no-ignore invocation must genuinely raise ``No source for code`` —
and only then proves the ignore-errors posture (the ``-i`` flag the shadow
aggregator uses, and the ``[report] ignore_errors`` config the authoritative
pytest-cov json inherits) makes generation succeed with real totals. A green on
absence would be vacuous, so we assert the RED path actually fails first.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import coverage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _dataset_with_missing_source(tmp_path: Path) -> tuple[Path, Path]:
    """Build a real ``.coverage`` dataset: one on-disk source + one missing .pyx.

    Returns ``(coverage_file, out_json)``. The missing ``providers.pyx`` is never
    written to disk, reproducing the F-18 condition exactly.
    """
    missing_pyx = tmp_path / "dependency_injector" / "providers.pyx"  # never created
    real_py = tmp_path / "real.py"
    real_py.write_text("x = 1\ny = 2\n", encoding="utf-8")

    coverage_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(coverage_file))
    data.add_lines({str(missing_pyx): [1, 2, 3], str(real_py): [1, 2]})
    data.write()
    return coverage_file, tmp_path / "out.json"


def _run_coverage_json(
    coverage_file: Path, out_json: Path, *, extra_args: list[str], rcfile: Path | None
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "coverage", "json", "-o", str(out_json), *extra_args]
    if rcfile is not None:
        cmd += ["--rcfile", str(rcfile)]
    return subprocess.run(
        cmd,
        cwd=str(coverage_file.parent),
        env={"COVERAGE_FILE": str(coverage_file), "PATH": _path_env()},
        capture_output=True,
        text=True,
    )


def _path_env() -> str:
    import os

    return os.environ.get("PATH", "")


class TestMissingSourceIsRedWithoutIgnore:
    """RED: the exact F-18 failure must reproduce without ignore-errors."""

    def test_no_ignore_raises_no_source(self, tmp_path: Path) -> None:
        coverage_file, out_json = _dataset_with_missing_source(tmp_path)

        proc = _run_coverage_json(coverage_file, out_json, extra_args=[], rcfile=None)

        assert proc.returncode != 0, (
            "coverage json unexpectedly succeeded on a missing-source dataset; "
            "the F-18 RED condition did not reproduce, so a green would be vacuous."
        )
        combined = proc.stderr + proc.stdout
        assert "No source for code" in combined
        assert "providers.pyx" in combined
        assert not out_json.exists()


class TestMissingSourceIsGreenWithIgnore:
    """GREEN: both ignore-errors postures produce a real census with totals."""

    def test_ignore_flag_succeeds(self, tmp_path: Path) -> None:
        """The ``-i`` flag — what aggregate_coverage_artifacts.py uses."""
        coverage_file, out_json = _dataset_with_missing_source(tmp_path)

        proc = _run_coverage_json(
            coverage_file, out_json, extra_args=["-i"], rcfile=None
        )

        assert proc.returncode == 0, proc.stderr
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        # The on-disk real.py is still measured — the census is not empty.
        assert payload["totals"]["num_statements"] >= 1

    def test_report_ignore_errors_config_succeeds(self, tmp_path: Path) -> None:
        """``[report] ignore_errors = True`` config — what pytest-cov's json inherits.

        This is the posture the F-18 fix installs via
        ``[tool.coverage.report] ignore_errors = true`` in pyproject.toml, so the
        authoritative ``run_coverage_sweep_gate.py`` (pytest-cov ``--cov-report=json``)
        matches the shadow aggregator's ``-i`` flag.
        """
        coverage_file, out_json = _dataset_with_missing_source(tmp_path)
        rcfile = tmp_path / ".coveragerc"
        rcfile.write_text("[report]\nignore_errors = True\n", encoding="utf-8")

        proc = _run_coverage_json(coverage_file, out_json, extra_args=[], rcfile=rcfile)

        assert proc.returncode == 0, proc.stderr
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["totals"]["num_statements"] >= 1


class TestRepoConfigCarriesIgnoreErrors:
    """The fix must be present in the repo's own pyproject — not just provable."""

    def test_pyproject_report_ignore_errors_true(self) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        report = data["tool"]["coverage"]["report"]
        assert report.get("ignore_errors") is True, (
            "pyproject.toml [tool.coverage.report] must set ignore_errors = true so "
            "the authoritative pytest-cov json report survives missing C-extension "
            "sources (F-18)."
        )
