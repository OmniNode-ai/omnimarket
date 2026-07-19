# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/run_shadow_slice.py (OMN-14775, friction F-23).

The shadow-slice / merge-proof harness used to enforce its per-slice timeout by
letting ``pytest-timeout``'s thread method call ``os._exit(1)``: the hung child
was orphaned/leaked, downstream results were masked, and the bare hard exit was
mis-classified as ``PRODUCT_FAILED``. This harness replaces that with a
signal-based timeout. These tests prove the three acceptance criteria:

  1. A hung slice is terminated via signals and the WHOLE process group is
     reaped — no orphaned/leaked grandchild (proven by a fork+heartbeat probe).
  2. The timeout emits leaked-thread / isolation diagnostics.
  3. The timeout output routes to ``RUNNER_INFRA`` through the REAL
     merge-controller classifier (a cross-boundary seam test), never
     ``PRODUCT_FAILED`` — and the RED contrast (no signature) shows the mask it
     replaces.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "ci"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import run_shadow_slice as harness  # noqa: E402

from omnimarket.merge_control.reason_code_classifier import (  # noqa: E402
    ALL_LOG_SIGNATURES,
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    classify,
)

# A product step name so a bare `failure` classifies as PRODUCT_FAILED unless a
# hang signature overrides it (matches the shadow's "Unit tests (fast slice)").
_PRODUCT_STEP = "Unit tests (fast slice)"

# Child that forks a grandchild heartbeat, so a leak leaves an observable writer.
_FORK_HEARTBEAT_PROG = r"""
import os, sys, time
marker, heartbeat = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    # grandchild: rewrite the heartbeat file until reaped
    while True:
        try:
            with open(heartbeat, "w") as fh:
                fh.write(str(time.time()))
        except OSError:
            pass
        time.sleep(0.05)
else:
    with open(marker, "w") as fh:
        fh.write("{},{}".format(os.getpid(), pid))
    while True:
        time.sleep(0.2)
"""


def _extract_signatures(log_text: str) -> tuple[str, ...]:
    """Mirror the inventory node's extraction (handler_pr_lifecycle_inventory
    ``_collect_flaky_failure_evidence``): the exact tuple fed to ``classify``."""
    lowered = log_text.lower()
    return tuple(sig for sig in ALL_LOG_SIGNATURES if sig in lowered)


class TestArgSplit:
    def test_splits_on_first_double_dash(self) -> None:
        opts, cmd = harness._split_args(["--timeout", "5", "--", "pytest", "-q"])
        assert opts == ["--timeout", "5"]
        assert cmd == ["pytest", "-q"]

    def test_no_double_dash_means_no_command(self) -> None:
        opts, cmd = harness._split_args(["--timeout", "5"])
        assert opts == ["--timeout", "5"]
        assert cmd == []

    def test_only_first_double_dash_splits(self) -> None:
        # A `--` inside the wrapped command must be preserved, not re-split.
        opts, cmd = harness._split_args(["--", "pytest", "--", "extra"])
        assert opts == []
        assert cmd == ["pytest", "--", "extra"]


class TestExitCodePassthrough:
    """A completing slice returns its OWN exit code — results are never masked."""

    def test_success_returns_zero(self) -> None:
        rc = harness.run_slice([sys.executable, "-c", "import sys; sys.exit(0)"], 30.0)
        assert rc == 0

    def test_failure_exit_code_is_preserved_not_hang(self) -> None:
        rc = harness.run_slice([sys.executable, "-c", "import sys; sys.exit(7)"], 30.0)
        assert rc == 7
        assert rc != harness.EXIT_HANG  # a real product red is NOT a hang

    def test_empty_command_is_usage_error(self) -> None:
        assert harness.run_slice([], 30.0) == 2

    def test_missing_binary_is_usage_error(self) -> None:
        assert harness.run_slice(["/nonexistent/binary/xyz"], 30.0) == 2


@pytest.mark.skipif(os.name != "posix", reason="process-group reaping is POSIX-only")
class TestHangReapAndClassification:
    def test_hang_is_reaped_no_leak_and_routes_to_runner_infra(
        self,
        tmp_path: Path,
        capfd: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Shorten the SIGABRT flush grace so the test stays fast.
        monkeypatch.setattr(harness, "_ABORT_GRACE_S", 0.3)
        marker = tmp_path / "marker.txt"
        heartbeat = tmp_path / "heartbeat.txt"

        rc = harness.run_slice(
            [
                sys.executable,
                "-c",
                _FORK_HEARTBEAT_PROG,
                str(marker),
                str(heartbeat),
            ],
            timeout_s=1.0,
        )

        # (acceptance 1a) the timeout is a distinct hang class, not exit 0/1.
        assert rc == harness.EXIT_HANG
        # the grandchild really forked (otherwise the no-leak proof is vacuous).
        assert marker.exists(), "grandchild never forked (marker missing)"
        assert marker.read_text().strip(), "grandchild never forked (marker empty)"

        # (acceptance 1b) NO leaked process: the grandchild's heartbeat has
        # stopped — if it were orphaned it would keep rewriting the file.
        assert heartbeat.exists()
        first = heartbeat.read_text()
        time.sleep(0.6)
        second = heartbeat.read_text()
        assert first == second, "grandchild still heartbeating — process leaked"

        out = capfd.readouterr()
        combined = (out.out + out.err).lower()
        # (acceptance 2) leaked-thread / isolation diagnostics were emitted.
        assert "hard timeout" in combined
        assert "leaked thread" in combined
        assert "reap" in combined

        # (acceptance 3) drive the REAL classifier via the REAL harness output,
        # through the same extraction the inventory node performs.
        extracted = _extract_signatures(combined)
        assert extracted, "harness output carried no classifier hang signature"
        facts = MergeCheckFacts(
            job_conclusion="failure",
            failed_step_name=_PRODUCT_STEP,
            log_signatures=extracted,
        )
        assert classify(facts) == EnumMergeCheckReasonCode.RUNNER_INFRA


class TestClassifierSeam:
    """Field-by-field seam against the live classifier signature set."""

    def test_harness_signatures_are_classifier_recognized(self) -> None:
        # Every phrase the harness emits must be a canonical classifier signature
        # (drift here silently re-masks a hang as a product failure).
        assert set(harness._HANG_SIGNATURES) <= set(ALL_LOG_SIGNATURES)

    def test_red_contrast_same_step_without_signature_is_product_failed(self) -> None:
        # Proves the signature is load-bearing (RED-vs-EXISTS-but-WRONG): the
        # identical failed product step with NO hang signature is the exact
        # mask this ticket removes — it classifies as PRODUCT_FAILED.
        masked = MergeCheckFacts(
            job_conclusion="failure",
            failed_step_name=_PRODUCT_STEP,
            log_signatures=(),
        )
        assert classify(masked) == EnumMergeCheckReasonCode.PRODUCT_FAILED

        fixed = MergeCheckFacts(
            job_conclusion="failure",
            failed_step_name=_PRODUCT_STEP,
            log_signatures=tuple(harness._HANG_SIGNATURES),
        )
        assert classify(fixed) == EnumMergeCheckReasonCode.RUNNER_INFRA
