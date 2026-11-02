# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the nightly chain-job silent-skip guard (OMN-16809).

The regression these encode
---------------------------
`delegation-regression-nightly.yml` is the platform's only scheduled live
delegation-chain check. Its pytest step reported ``success`` for seven
consecutive nights (2026-08-21 .. 2026-08-27) while executing **zero** live
cases, because ``TestDelegationGoldenTasksLive`` self-skips and an all-skipped
pytest session exits 0.

Two distinct skip paths produce that silence, and the JUnit fixtures below carry
their *verbatim* reasons from ``tests/delegation_golden/test_layer2_live_runner.py``:

* the class-level ``skipif`` on ``OMN_ALLOW_LIVE_E2E_PROBE`` (lines 190-197)
* the inner ``pytest.skip`` on an unset lane Postgres password (lines 217-221)

Why a second config rather than reusing the merge-gating one
------------------------------------------------------------
``integration_skip_guard.yaml`` (OMN-14172) lists ``OMN_ALLOW_LIVE`` and
``set OMN_ALLOW`` under ``allowed_optional_skip_patterns``. That is *correct* on
a PR job, which deliberately provisions no lane, and exactly wrong on the
nightly, which sets that flag itself and does provision one. The
``test_merge_gating_config_*`` tests below pin both halves: the merge-gating
config keeps its optional classification (AC5), and it demonstrably would NOT
have caught the class-level skip — which is the whole reason the nightly needs
its own config where nothing is optional.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_integration_skips_check_compute.runtime_integration_skips_check import (
    main,
)

# OMN-21940: the guard is omnibase_core's integration-skip-guard runtime; this repo
# supplies the two configs (merge-gating and nightly) and the workflow wiring.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NIGHTLY_CONFIG = _REPO_ROOT / "scripts" / "ci" / "nightly_chain_skip_guard.yaml"
_MERGE_GATING_CONFIG = _REPO_ROOT / "scripts" / "ci" / "integration_skip_guard.yaml"
_NIGHTLY_WORKFLOW = (
    _REPO_ROOT / ".github" / "workflows" / "delegation-regression-nightly.yml"
)

# Verbatim from test_layer2_live_runner.py:192-196 — the class-level skipif reason.
_REASON_LIVE_FLAG_UNSET = (
    "Requires OMN_ALLOW_LIVE_E2E_PROBE=true to run against the live bus. "
    "Set it explicitly to execute the nightly golden-task probe."
)

# Verbatim from test_layer2_live_runner.py:218-221 — the inner pytest.skip reason.
_REASON_LANE_PASSWORD_UNSET = (
    "ONEX_E2E_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set — "
    # Host/port are placeholders: the guard matches the reason PREFIX, never the
    # address, so no real lane literal belongs in a synthetic fixture.
    "cannot connect to Postgres at lane-postgres.invalid:5432"
)

# The shape of the 7 silent nights: every live case skipped, nothing executed.
_JUNIT_ALL_SKIPPED = f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="2">
 <testcase classname="tests.delegation_golden.test_layer2_live_runner.TestDelegationGoldenTasksLive"
   name="test_case_behaves_as_expected[I1]" time="0.0">
   <skipped type="pytest.skip" message="{_REASON_LIVE_FLAG_UNSET}"/>
 </testcase>
 <testcase classname="tests.delegation_golden.test_layer2_live_runner.TestDelegationGoldenTasksLive"
   name="test_case_behaves_as_expected[I2]" time="0.0">
   <skipped type="pytest.skip" message="{_REASON_LANE_PASSWORD_UNSET}"/>
 </testcase>
</testsuite></testsuites>"""

# A genuine live night: the lane was reachable and the cases actually ran.
_JUNIT_LIVE_RUN = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="0">
 <testcase classname="tests.delegation_golden.test_layer2_live_runner.TestDelegationGoldenTasksLive"
   name="test_case_behaves_as_expected[I1]" time="4.2"/>
 <testcase classname="tests.delegation_golden.test_layer2_live_runner.TestDelegationGoldenTasksLive"
   name="test_case_behaves_as_expected[I2]" time="3.9"/>
</testsuite></testsuites>"""

# Zero collection: a marker typo or a broken selector picks nothing at all. The
# report is well-formed and contains no failures, so it reads as a pass.
_JUNIT_ZERO_COLLECTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="0" skipped="0"></testsuite></testsuites>"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _junit_one_skip(reason: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="1" skipped="1">
 <testcase classname="tests.delegation_golden.test_layer2_live_runner.TestDelegationGoldenTasksLive"
   name="test_case_behaves_as_expected[I1]" time="0.0">
   <skipped type="pytest.skip" message="{reason}"/>
 </testcase>
</testsuite></testsuites>"""


def _verdict(
    tmp_path: Path, body: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    report = _write(tmp_path, "report.xml", body)
    rc = main(["--junit", str(report), "--config", str(config)])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


@pytest.mark.unit
class TestNightlyConfigClassification:
    """AC1 — both observed skip reasons are false-greens, not optional skips."""

    def test_live_flag_skip_is_a_false_green(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _verdict(
            tmp_path, _junit_one_skip(_REASON_LIVE_FLAG_UNSET), _NIGHTLY_CONFIG, capsys
        )
        assert rc == 1, (
            "the nightly SETS OMN_ALLOW_LIVE_E2E_PROBE itself; a skip citing it "
            "unset means the job env broke, which is a false-green, not an opt-out"
        )
        assert "FALSE-GREEN" in out

    def test_lane_password_skip_is_a_false_green(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _verdict(
            tmp_path,
            _junit_one_skip(_REASON_LANE_PASSWORD_UNSET),
            _NIGHTLY_CONFIG,
            capsys,
        )
        assert rc == 1
        assert "FALSE-GREEN" in out

    def test_nothing_is_optional_on_the_nightly(self) -> None:
        config = yaml.safe_load(_NIGHTLY_CONFIG.read_text(encoding="utf-8"))
        assert not config.get("allowed_optional_skip_patterns"), (
            "the nightly provisions the lane it probes; no skip on it is legitimate"
        )
        assert config["silent_skip_allowed"] is False
        assert config["require_executed_min"] >= 1


@pytest.mark.unit
class TestNightlyGateVerdicts:
    """AC2 — an all-skipped or zero-collected session must exit non-zero."""

    def test_all_skipped_night_turns_the_gate_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _verdict(tmp_path, _JUNIT_ALL_SKIPPED, _NIGHTLY_CONFIG, capsys)
        assert rc == 1, "seven green nights over zero live cases is the defect"
        assert "ZERO/UNDER-COLLECTION" in out
        assert "FALSE-GREEN" in out

    def test_zero_collected_night_turns_the_gate_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _verdict(tmp_path, _JUNIT_ZERO_COLLECTED, _NIGHTLY_CONFIG, capsys)
        assert rc == 1
        assert "ZERO/UNDER-COLLECTION" in out

    def test_genuine_live_night_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _verdict(tmp_path, _JUNIT_LIVE_RUN, _NIGHTLY_CONFIG, capsys)
        assert rc == 0, (
            f"a real live run must not be flagged; the gate has to stay usable: {out}"
        )


@pytest.mark.unit
class TestNightlyGateFailsClosed:
    """AC3 — an absent or unreadable report is a failure, never a pass."""

    def test_missing_report_fails_closed(self, tmp_path: Path) -> None:
        rc = main(
            [
                "--junit",
                str(tmp_path / "never_written.xml"),
                "--config",
                str(_NIGHTLY_CONFIG),
            ]
        )
        assert rc == 2, "a pytest step that crashed before writing junit must not pass"

    def test_unparsable_report_fails_closed(self, tmp_path: Path) -> None:
        report = _write(tmp_path, "truncated.xml", "<testsuites><testsuite>")
        rc = main(["--junit", str(report), "--config", str(_NIGHTLY_CONFIG)])
        assert rc == 2


@pytest.mark.unit
class TestMergeGatingConfigUnchanged:
    """AC5 — and the demonstration of why a second config was necessary."""

    def test_merge_gating_still_treats_live_optin_as_optional(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="1">
 <testcase classname="tests.live.TestLive" name="test_probe" time="0.0">
   <skipped type="pytest.skip" message="live e2e probe; set OMN_ALLOW_LIVE_E2E_PROBE=true to enable"/>
 </testcase>
 <testcase classname="tests.live.TestLive" name="test_executed" time="0.1"/>
</testsuite></testsuites>"""
        rc, out = _verdict(tmp_path, body, _MERGE_GATING_CONFIG, capsys)
        assert rc == 0, (
            "on a PR job no lane is provisioned, so this skip is legitimate — "
            f"OMN-14172 behaviour must not regress: {out}"
        )

    def test_merge_gating_config_would_not_have_caught_the_class_level_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = (
            _JUNIT_ALL_SKIPPED.replace(
                _REASON_LANE_PASSWORD_UNSET, _REASON_LIVE_FLAG_UNSET
            )
            .replace('tests="2" skipped="2"', 'tests="3" skipped="2"')
            .replace(
                "</testsuite>",
                '<testcase classname="tests.live.TestLive" name="test_executed" time="0.1"/>'
                "</testsuite>",
            )
        )
        merge_rc, merge_out = _verdict(tmp_path, body, _MERGE_GATING_CONFIG, capsys)
        nightly_rc, _ = _verdict(tmp_path, body, _NIGHTLY_CONFIG, capsys)
        # This is the reason the nightly cannot simply reuse the merge-gating
        # config: it classifies the exact skip that produced seven silent nights
        # as legitimately optional.
        assert merge_rc == 0, merge_out
        assert nightly_rc == 1


@pytest.mark.unit
class TestNightlyWorkflowWiring:
    """AC4 — the wiring itself is pinned so the guard cannot be silently removed."""

    def test_pytest_step_emits_a_junit_report_the_guard_reads(self) -> None:
        body = _NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        assert "--junitxml=" in body, "no report means nothing to evaluate"
        assert "runtime_integration_skips_check" in body
        assert "nightly_chain_skip_guard.yaml" in body, (
            "the guard must run against the nightly config, not the merge-gating one"
        )

    def test_guard_step_runs_even_when_pytest_fails(self) -> None:
        body = _NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        guard_idx = body.index("runtime_integration_skips_check")
        # The guard step's own `if:` must precede its run block.
        preceding = body[:guard_idx]
        step_start = preceding.rindex("- name:")
        assert "if: always()" in body[step_start:guard_idx], (
            "a guard that only runs on green cannot report the all-skipped case"
        )
