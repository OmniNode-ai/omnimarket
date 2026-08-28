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

from scripts.ci.check_integration_skips import (
    GuardConfig,
    classify_skip,
    evaluate,
    main,
    parse_junit,
)

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
    "cannot connect to Postgres at 192.168.86.201:15436"
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


@pytest.fixture
def nightly_cfg() -> GuardConfig:
    return GuardConfig.load(_NIGHTLY_CONFIG)


@pytest.fixture
def merge_gating_cfg() -> GuardConfig:
    return GuardConfig.load(_MERGE_GATING_CONFIG)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.unit
class TestNightlyConfigClassification:
    """AC1 — both observed skip reasons are false-greens, not optional skips."""

    def test_live_flag_skip_is_a_false_green(self, nightly_cfg: GuardConfig) -> None:
        offending, is_allowed = classify_skip(_REASON_LIVE_FLAG_UNSET, nightly_cfg)
        assert offending is not None, (
            "the nightly SETS OMN_ALLOW_LIVE_E2E_PROBE itself; a skip citing it "
            "unset means the job env broke, which is a false-green, not an opt-out"
        )
        assert not is_allowed

    def test_lane_password_skip_is_a_false_green(
        self, nightly_cfg: GuardConfig
    ) -> None:
        offending, is_allowed = classify_skip(_REASON_LANE_PASSWORD_UNSET, nightly_cfg)
        assert offending is not None
        assert not is_allowed

    def test_nothing_is_optional_on_the_nightly(self, nightly_cfg: GuardConfig) -> None:
        assert nightly_cfg.allowed_optional_patterns == [], (
            "the nightly provisions the lane it probes; no skip on it is legitimate"
        )
        assert nightly_cfg.silent_skip_allowed is False
        assert nightly_cfg.require_executed_min >= 1


@pytest.mark.unit
class TestNightlyGateVerdicts:
    """AC2 — an all-skipped or zero-collected session must exit non-zero."""

    def test_all_skipped_night_turns_the_gate_red(
        self, tmp_path: Path, nightly_cfg: GuardConfig
    ) -> None:
        report = _write(tmp_path, "all_skipped.xml", _JUNIT_ALL_SKIPPED)
        violations = evaluate(parse_junit([report]), nightly_cfg, strict=False)
        assert violations, "seven green nights over zero live cases is the defect"
        joined = " ".join(violations)
        assert "ZERO/UNDER-COLLECTION" in joined
        assert "FALSE-GREEN" in joined

    def test_all_skipped_night_exits_non_zero_through_the_cli(
        self, tmp_path: Path
    ) -> None:
        report = _write(tmp_path, "all_skipped.xml", _JUNIT_ALL_SKIPPED)
        rc = main(["--junit", str(report), "--config", str(_NIGHTLY_CONFIG)])
        assert rc == 1, "the workflow step reads this exit code; 0 is the false-green"

    def test_zero_collected_night_turns_the_gate_red(
        self, tmp_path: Path, nightly_cfg: GuardConfig
    ) -> None:
        report = _write(tmp_path, "zero.xml", _JUNIT_ZERO_COLLECTED)
        violations = evaluate(parse_junit([report]), nightly_cfg, strict=False)
        assert any("ZERO/UNDER-COLLECTION" in v for v in violations)

    def test_genuine_live_night_passes(self, tmp_path: Path) -> None:
        report = _write(tmp_path, "live.xml", _JUNIT_LIVE_RUN)
        rc = main(["--junit", str(report), "--config", str(_NIGHTLY_CONFIG)])
        assert rc == 0, (
            "a real live run must not be flagged; the gate has to stay usable"
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
        self, merge_gating_cfg: GuardConfig
    ) -> None:
        offending, is_allowed = classify_skip(
            "live e2e probe; set OMN_ALLOW_LIVE_E2E_PROBE=true to enable",
            merge_gating_cfg,
        )
        assert offending is None
        assert is_allowed, (
            "on a PR job no lane is provisioned, so this skip is legitimate — "
            "OMN-14172 behaviour must not regress"
        )

    def test_merge_gating_config_would_not_have_caught_the_class_level_skip(
        self, merge_gating_cfg: GuardConfig
    ) -> None:
        offending, is_allowed = classify_skip(_REASON_LIVE_FLAG_UNSET, merge_gating_cfg)
        # This is the reason the nightly cannot simply reuse the merge-gating
        # config: it classifies the exact skip that produced seven silent nights
        # as legitimately optional.
        assert offending is None
        assert is_allowed


@pytest.mark.unit
class TestNightlyWorkflowWiring:
    """AC4 — the wiring itself is pinned so the guard cannot be silently removed."""

    def test_pytest_step_emits_a_junit_report_the_guard_reads(self) -> None:
        body = _NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        assert "--junitxml=" in body, "no report means nothing to evaluate"
        assert "check_integration_skips.py" in body
        assert "nightly_chain_skip_guard.yaml" in body, (
            "the guard must run against the nightly config, not the merge-gating one"
        )

    def test_guard_step_runs_even_when_pytest_fails(self) -> None:
        body = _NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        guard_idx = body.index("check_integration_skips.py")
        # The guard step's own `if:` must precede its run block.
        preceding = body[:guard_idx]
        step_start = preceding.rindex("- name:")
        assert "if: always()" in body[step_start:guard_idx], (
            "a guard that only runs on green cannot report the all-skipped case"
        )
