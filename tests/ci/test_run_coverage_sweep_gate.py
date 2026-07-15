# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/run_coverage_sweep_gate.py (OMN-14539).

MANDATORY RED PROOF (per the class fix, OMN-14531): the gate must go RED
against an EXISTS-but-WRONG scope — a real target dir that legitimately
exists but has no coverage census — and GREEN only against a genuinely
populated, freshly measured coverage.json. A green on absence is vacuous.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))


def _write_coverage_json(target_dir: Path, files: dict[str, dict[str, object]]) -> None:
    (target_dir / "coverage.json").write_text(json.dumps({"files": files}))


class TestCoverageSweepGateRedProof:
    """RED: an EXISTS-but-WRONG scope (real dir, no coverage census) must fail."""

    def test_missing_coverage_json_is_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A real, existing target dir with NO coverage.json must exit 1.

        This is the exact defect from OMN-14531: the repo dir is a
        legitimate scan target, but the census artifact backing it does not
        exist. Before OMN-14539 the handler silently `continue`d past this
        and reported status="clean" — a false green over zero measured
        modules. The gate must now refuse to pass.
        """
        from run_coverage_sweep_gate import main

        rc = main(["--target-dir", str(tmp_path), "--skip-generate"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["total_modules"] == 0
        assert payload["coverage_missing"]

    def test_corrupt_coverage_json_is_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A present but unparseable coverage.json must also fail, not skip."""
        from run_coverage_sweep_gate import main

        (tmp_path / "coverage.json").write_text("{not valid json")

        rc = main(["--target-dir", str(tmp_path), "--skip-generate"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["coverage_missing"]

    def test_empty_files_census_is_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """coverage.json present but with zero measured files is still a FAIL.

        repos_scanned > 0 with total_modules == 0 must never be reported
        clean — the scope was scanned but nothing was ever measured.
        """
        from run_coverage_sweep_gate import main

        _write_coverage_json(tmp_path, {})

        rc = main(["--target-dir", str(tmp_path), "--skip-generate"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["total_modules"] == 0


class TestCoverageSweepGateGreenProof:
    """GREEN: a genuinely populated, real coverage.json must pass."""

    def test_healthy_populated_scope_is_green(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from run_coverage_sweep_gate import main

        _write_coverage_json(
            tmp_path,
            {
                "src/a.py": {
                    "summary": {
                        "percent_covered": 90.0,
                        "num_statements": 20,
                        "missing_lines": 2,
                    }
                },
                "src/b.py": {
                    "summary": {
                        "percent_covered": 80.0,
                        "num_statements": 10,
                        "missing_lines": 2,
                    }
                },
            },
        )

        rc = main(
            ["--target-dir", str(tmp_path), "--skip-generate", "--target-pct", "50"]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "clean"
        assert payload["total_modules"] == 2
        assert payload["repos_scanned"] == 1
        assert not payload["coverage_missing"]

    def test_gaps_found_is_still_green_existing_debt_not_blocking(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A genuinely measured scope with below-threshold modules still
        exits 0 — coverage debt is informational (ticketed downstream), not
        a merge blocker. Only an UNMEASURED scope fails this gate."""
        from run_coverage_sweep_gate import main

        _write_coverage_json(
            tmp_path,
            {
                "src/low.py": {
                    "summary": {
                        "percent_covered": 10.0,
                        "num_statements": 40,
                        "missing_lines": 36,
                    }
                }
            },
        )

        rc = main(
            ["--target-dir", str(tmp_path), "--skip-generate", "--target-pct", "50"]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "gaps_found"
        assert payload["total_modules"] == 1


class TestCoverageSweepGateGenerationWiring:
    """The generation step is real subprocess-driven code, not prose."""

    def test_generate_helper_invokes_pytest_cov(self, tmp_path: Path) -> None:
        from run_coverage_sweep_gate import generate_coverage_json

        (tmp_path / "src").mkdir()

        captured_cmd: list[str] = []

        captured_kwargs: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured_cmd.extend(cmd)
            captured_kwargs.update(kwargs)
            (tmp_path / "coverage.json").write_text(json.dumps({"files": {}}))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("run_coverage_sweep_gate.subprocess.run", side_effect=_fake_run):
            ok, message = generate_coverage_json(tmp_path)

        assert ok is True
        assert "coverage.json" in message
        assert "pytest" in captured_cmd
        assert any("--cov-report=json" in part for part in captured_cmd)
        assert "capture_output" not in captured_kwargs
        assert "stdout" not in captured_kwargs
        assert "stderr" not in captured_kwargs

    def test_generate_helper_reports_failure_without_swallowing(
        self, tmp_path: Path
    ) -> None:
        from run_coverage_sweep_gate import generate_coverage_json

        def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with patch("run_coverage_sweep_gate.subprocess.run", side_effect=_fake_run):
            ok, message = generate_coverage_json(tmp_path)

        assert ok is False
        assert "boom" in message or "coverage.json" in message

    def test_generation_failure_exits_two(self, tmp_path: Path) -> None:
        from run_coverage_sweep_gate import main

        with patch(
            "run_coverage_sweep_gate.generate_coverage_json",
            return_value=(False, "engine exploded"),
        ):
            rc = main(["--target-dir", str(tmp_path)])

        assert rc == 2
