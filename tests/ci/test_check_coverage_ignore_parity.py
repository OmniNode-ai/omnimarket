# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/check_coverage_ignore_parity.py (OMN-14762 / F-18).

The gate keeps the authoritative pytest-cov json (config-driven ignore-errors)
aligned with the shadow aggregator's ``coverage json -i``. RED when either side
drops ignore-errors; GREEN when both carry it; GREEN against the real repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

from check_coverage_ignore_parity import main  # noqa: E402

_AGG_WITH_IGNORE = 'cmd = [py, "-m", "coverage", "json", "-i", "-o", str(out_path)]\n'
_AGG_WITHOUT_IGNORE = 'cmd = [py, "-m", "coverage", "json", "-o", str(out_path)]\n'
_PYPROJECT_WITH = "[tool.coverage.report]\nshow_missing = true\nignore_errors = true\n"
_PYPROJECT_WITHOUT = "[tool.coverage.report]\nshow_missing = true\n"


def _make_root(tmp_path: Path, *, pyproject: str, aggregate: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    ci_dir = tmp_path / "scripts" / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "aggregate_coverage_artifacts.py").write_text(aggregate, encoding="utf-8")
    return tmp_path


class TestParityGate:
    def test_green_when_both_carry_ignore(self, tmp_path: Path) -> None:
        root = _make_root(
            tmp_path, pyproject=_PYPROJECT_WITH, aggregate=_AGG_WITH_IGNORE
        )
        assert main(["--repo-root", str(root)]) == 0

    def test_red_when_pyproject_missing_ignore(self, tmp_path: Path) -> None:
        root = _make_root(
            tmp_path, pyproject=_PYPROJECT_WITHOUT, aggregate=_AGG_WITH_IGNORE
        )
        assert main(["--repo-root", str(root)]) == 1

    def test_red_when_aggregator_drops_ignore_flag(self, tmp_path: Path) -> None:
        root = _make_root(
            tmp_path, pyproject=_PYPROJECT_WITH, aggregate=_AGG_WITHOUT_IGNORE
        )
        assert main(["--repo-root", str(root)]) == 1

    def test_json_section_ignore_errors_also_accepted(self, tmp_path: Path) -> None:
        root = _make_root(
            tmp_path,
            pyproject="[tool.coverage.json]\nignore_errors = true\n",
            aggregate=_AGG_WITH_IGNORE,
        )
        assert main(["--repo-root", str(root)]) == 0

    def test_long_form_ignore_errors_flag_accepted(self, tmp_path: Path) -> None:
        root = _make_root(
            tmp_path,
            pyproject=_PYPROJECT_WITH,
            aggregate='cmd = [py, "-m", "coverage", "json", "--ignore-errors", "-o", x]\n',
        )
        assert main(["--repo-root", str(root)]) == 0


class TestRealRepo:
    def test_real_repo_is_green(self) -> None:
        # The F-18 fix is present in this repo: pyproject ignore_errors=true and
        # aggregate_coverage_artifacts.py's `coverage json -i`.
        assert main(["--repo-root", str(REPO_ROOT)]) == 0
