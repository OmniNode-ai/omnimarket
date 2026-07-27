# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the integration silent-skip false-green gate (OMN-14172).

Encodes the regression the gate exists to prevent: an `@pytest.mark.integration`
real-service test that self-skips because a PROVISIONED service (Postgres) looks
absent must turn the gate RED (case b), while a job where the same tests actually
ran — plus a legitimately-optional live-e2e skip — must PASS (case a).

The synthetic JUnit fixtures below mirror the exact schema pytest emits for the
two real omnimarket proofs (``test_delegation_savings_tenant_id_column_omn14058``
and ``test_projection_delegation_tier_distribution_omn13662``) — confirmed by
running real pytest and inspecting the ``<skipped message=...>`` element.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_integration_skips import (
    GuardConfig,
    evaluate,
    main,
    parse_junit,
    selftest,
)

_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "integration_skip_guard.yaml"
)

# Postgres provisioned: the two real-DB proofs RAN; only a live-e2e opt-in skips.
_JUNIT_PROVISIONED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" skipped="1">
 <testcase classname="tests.test_delegation_savings_tenant_id_column_omn14058"
   name="test_delegation_events_upsert_stamps_tenant_id_on_real_postgres" time="0.1"/>
 <testcase classname="tests.test_projection_delegation_tier_distribution_omn13662"
   name="test_view_classifies_not_tier_routed_against_real_postgres" time="0.1"/>
 <testcase classname="tests.integration.e2e_probe.test_delegation_e2e_probe" name="test_probe">
   <skipped type="pytest.skip" message="live e2e probe; set OMN_ALLOW_LIVE_E2E_PROBE=true to enable"/>
 </testcase>
</testsuite></testsuites>"""

# Postgres removed / POSTGRES_PASSWORD unset: both real-DB proofs SILENTLY SKIP.
# This is the exact false-green that let the OMN-14058 missing-tenant_id-column
# defect reach dev.
_JUNIT_SILENT_SKIP = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="2">
 <testcase classname="tests.test_delegation_savings_tenant_id_column_omn14058"
   name="test_delegation_events_upsert_stamps_tenant_id_on_real_postgres" time="0.0">
   <skipped type="pytest.skip" message="POSTGRES_PASSWORD not set — skipping tenant_id column DB proof"/>
 </testcase>
 <testcase classname="tests.test_projection_delegation_tier_distribution_omn13662"
   name="test_view_classifies_not_tier_routed_against_real_postgres" time="0.0">
   <skipped type="pytest.skip" message="no reachable Postgres for tier-distribution DB proof: Connection refused"/>
 </testcase>
</testsuite></testsuites>"""


@pytest.fixture
def cfg() -> GuardConfig:
    return GuardConfig.load(_CONFIG)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.unit
def test_shipped_config_is_fail_closed(cfg: GuardConfig) -> None:
    assert cfg.silent_skip_allowed is False
    assert "postgres" in cfg.required_service_patterns
    assert cfg.required_service_patterns["postgres"], "postgres needs skip patterns"
    assert cfg.require_executed_min >= 1


@pytest.mark.unit
def test_case_a_provisioned_service_passes(cfg: GuardConfig, tmp_path: Path) -> None:
    junit = _write(tmp_path, "a.xml", _JUNIT_PROVISIONED)
    stats = parse_junit([junit])
    assert stats.executed == 2  # both real-DB proofs ran
    assert evaluate(stats, cfg, strict=False) == []


@pytest.mark.unit
def test_case_b_silent_skip_goes_red(cfg: GuardConfig, tmp_path: Path) -> None:
    junit = _write(tmp_path, "b.xml", _JUNIT_SILENT_SKIP)
    stats = parse_junit([junit])
    assert stats.executed == 0
    violations = evaluate(stats, cfg, strict=False)
    assert violations, "reintroduced missing-service silent skip must be caught"
    joined = " ".join(violations)
    assert "FALSE-GREEN" in joined
    assert "postgres" in joined


@pytest.mark.unit
def test_case_b_main_exit_code_is_nonzero(cfg: GuardConfig, tmp_path: Path) -> None:
    junit = _write(tmp_path, "b.xml", _JUNIT_SILENT_SKIP)
    assert main(["--junit", str(junit), "--config", str(_CONFIG)]) == 1


@pytest.mark.unit
def test_case_a_main_exit_code_is_zero(cfg: GuardConfig, tmp_path: Path) -> None:
    junit = _write(tmp_path, "a.xml", _JUNIT_PROVISIONED)
    assert main(["--junit", str(junit), "--config", str(_CONFIG)]) == 0


@pytest.mark.unit
def test_live_optional_skip_is_not_a_false_green(
    cfg: GuardConfig, tmp_path: Path
) -> None:
    body = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="1">
 <testcase classname="t" name="ran" time="0.1"/>
 <testcase classname="t" name="live"><skipped type="pytest.skip"
   message="live cloud delegation call; set OMN_ALLOW_LIVE_CLOUD_DELEGATION=1 to enable"/></testcase>
</testsuite></testsuites>"""
    junit = _write(tmp_path, "opt.xml", body)
    assert evaluate(parse_junit([junit]), cfg, strict=False) == []


@pytest.mark.unit
def test_kafka_broker_skip_is_allowed(cfg: GuardConfig, tmp_path: Path) -> None:
    body = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="1">
 <testcase classname="t" name="ran" time="0.1"/>
 <testcase classname="t" name="kafka"><skipped type="pytest.skip"
   message="Redpanda broker not reachable at localhost:9092: connection refused"/></testcase>
</testsuite></testsuites>"""
    junit = _write(tmp_path, "kafka.xml", body)
    assert evaluate(parse_junit([junit]), cfg, strict=False) == []


@pytest.mark.unit
def test_zero_collection_is_a_false_green(cfg: GuardConfig, tmp_path: Path) -> None:
    body = '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="0"/></testsuites>'
    junit = _write(tmp_path, "empty.xml", body)
    violations = evaluate(parse_junit([junit]), cfg, strict=False)
    assert any("UNDER-COLLECTION" in v for v in violations)


@pytest.mark.unit
def test_missing_report_fails_closed(cfg: GuardConfig) -> None:
    assert main(["--junit", "/nonexistent/nope.xml", "--config", str(_CONFIG)]) == 2


@pytest.mark.unit
def test_strict_flags_unclassified_skip(cfg: GuardConfig, tmp_path: Path) -> None:
    body = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" skipped="1">
 <testcase classname="t" name="ran" time="0.1"/>
 <testcase classname="t" name="weird"><skipped type="pytest.skip"
   message="some brand new unrecognised reason nobody classified yet"/></testcase>
</testsuite></testsuites>"""
    junit = _write(tmp_path, "weird.xml", body)
    assert evaluate(parse_junit([junit]), cfg, strict=False) == []
    assert any(
        "UNCLASSIFIED-SKIP" in v
        for v in evaluate(parse_junit([junit]), cfg, strict=True)
    )


@pytest.mark.unit
def test_embedded_selftest_passes(cfg: GuardConfig) -> None:
    assert selftest(cfg) == 0
