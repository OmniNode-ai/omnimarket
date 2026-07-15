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


class _FakePopen:
    """Minimal stand-in for ``subprocess.Popen`` used by generate_coverage_json.

    OMN-14645 (+ deconflict with OMN-14641 #1776): generation STREAMS the
    child's stdout/stderr live to the job log (no ``capture_output`` / PIPE) and
    emits parent heartbeat output while the child is still running;
    ``start_new_session=True`` still lets a timeout reap the whole process
    group. These fakes exercise that path without a real subprocess. ``pid`` is
    a non-existent value so the best-effort ``_reap_process_group`` helper
    resolves to a no-op (getpgid raises).
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.args = cmd
        self.pid = 2**31 - 1  # non-existent pid; reaping is a guarded no-op
        self.returncode = returncode
        self._timeout = timeout

    def wait(self, timeout=None):  # type: ignore[no-untyped-def]
        if self._timeout:
            self._timeout = False  # a subsequent wait after reaping returns
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.returncode

    def poll(self) -> int | None:
        if self._timeout:
            return None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class TestCoverageSweepGateGenerationWiring:
    """The generation step is real subprocess-driven code, not prose."""

    def test_generate_helper_invokes_pytest_cov(self, tmp_path: Path) -> None:
        from run_coverage_sweep_gate import generate_coverage_json

        (tmp_path / "src").mkdir()

        captured_cmd: list[str] = []
        captured_kwargs: dict[str, object] = {}

        def _fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured_cmd.extend(cmd)
            captured_kwargs.update(kwargs)
            # OMN-14645: the child MUST be started in its own session so a
            # timeout can reap the whole process group.
            assert kwargs.get("start_new_session") is True
            (tmp_path / "coverage.json").write_text(json.dumps({"files": {}}))
            return _FakePopen(cmd, returncode=0)

        with (
            patch("run_coverage_sweep_gate.subprocess.Popen", side_effect=_fake_popen),
            patch("run_coverage_sweep_gate.time.sleep"),
        ):
            ok, message = generate_coverage_json(tmp_path, heartbeat_s=1)

        assert ok is True
        assert "coverage.json" in message
        assert "pytest" in captured_cmd
        assert any("--cov-report=json" in part for part in captured_cmd)
        # OMN-14645 / deconflict OMN-14641 (#1776): output MUST stream to the
        # job log, never be captured/PIPE-buffered — capturing freezes the run's
        # updatedAt and re-triggers the Codex stale-run cancel storm this fixes.
        assert "capture_output" not in captured_kwargs
        assert "stdout" not in captured_kwargs
        assert "stderr" not in captured_kwargs

    def test_generate_helper_reports_failure_without_swallowing(
        self, tmp_path: Path
    ) -> None:
        from run_coverage_sweep_gate import generate_coverage_json

        def _fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            # exits non-zero and writes NO coverage.json
            return _FakePopen(cmd, returncode=1)

        with patch("run_coverage_sweep_gate.subprocess.Popen", side_effect=_fake_popen):
            ok, message = generate_coverage_json(tmp_path)

        assert ok is False
        # The failing subprocess streamed its own diagnostics to the job log;
        # the returned message just points there — it must not silently swallow.
        assert "streamed pytest output" in message

    def test_generation_timeout_reaps_and_fails_loudly(self, tmp_path: Path) -> None:
        """OMN-14645: a timed-out generation reaps the process group and reports
        failure LOUDLY — it must never leave a stale artifact swept as clean.

        The timeout path is driven by ``proc.wait(timeout=...)`` raising
        ``TimeoutExpired`` (streaming, no capture), NOT by ``communicate``.
        """
        from run_coverage_sweep_gate import generate_coverage_json

        reaped: dict[str, bool] = {"called": False}

        def _fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return _FakePopen(cmd, timeout=True)

        def _fake_reap(proc):  # type: ignore[no-untyped-def]
            reaped["called"] = True

        with (
            patch("run_coverage_sweep_gate.subprocess.Popen", side_effect=_fake_popen),
            patch(
                "run_coverage_sweep_gate._reap_process_group", side_effect=_fake_reap
            ),
        ):
            ok, message = generate_coverage_json(tmp_path, timeout_s=1)

        assert ok is False
        assert "timed out" in message
        # Reaping-on-timeout is retained and fires loudly (no silent stale pass).
        assert reaped["called"] is True

    def test_generate_helper_emits_parent_heartbeat(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from run_coverage_sweep_gate import generate_coverage_json

        class _FakeProcess:
            returncode = 0

            def __init__(self) -> None:
                self._poll_count = 0

            def poll(self) -> int | None:
                self._poll_count += 1
                if self._poll_count < 3:
                    return None
                (tmp_path / "coverage.json").write_text(json.dumps({"files": {}}))
                return 0

        times = iter([0.0, 1.0, 1.0, 2.0, 2.0])

        with (
            patch(
                "run_coverage_sweep_gate.subprocess.Popen", return_value=_FakeProcess()
            ),
            patch(
                "run_coverage_sweep_gate.time.monotonic",
                side_effect=lambda: next(times),
            ),
            patch("run_coverage_sweep_gate.time.sleep"),
        ):
            ok, _ = generate_coverage_json(tmp_path, heartbeat_s=1)

        assert ok is True
        assert "coverage generation still running after 1s" in capsys.readouterr().out

    def test_generation_failure_exits_two(self, tmp_path: Path) -> None:
        from run_coverage_sweep_gate import main

        with patch(
            "run_coverage_sweep_gate.generate_coverage_json",
            return_value=(False, "engine exploded"),
        ):
            rc = main(["--target-dir", str(tmp_path)])

        assert rc == 2
