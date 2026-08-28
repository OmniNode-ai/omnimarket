#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14172 — silent-skip false-green prevention gate.

An `@pytest.mark.integration` real-service test that self-skips because its
DB/service/env is absent produces a *false-green*: the required Tests job
reports success while the real-DB assertion never ran. On a merge-gating job
that ACTUALLY provisions that service (Postgres, per OMN-14167), such a skip is
a defect the gate must turn RED.

This script parses the pytest JUnit-XML report produced by the guard job (which
runs `-m "integration and not kafka"` WITH Postgres provisioned) and fails when:

  1. any integration test was SKIPPED citing a *provisioned* service's absence
     (`required_services[*].missing_skip_patterns`), or
  2. fewer than `require_executed_min` integration tests actually EXECUTED
     (guards the zero-collection / all-skip false-green — a marker typo or a
     broken selector collecting nothing).

Legitimately-optional skips — Kafka broker (not provisioned by design), live-LLM
/ live-e2e opt-in flags — are never treated as false-greens, because those
resources are deliberately not provisioned on the merge-gating job.

Usage
-----
    check_integration_skips.py --junit FILE [FILE ...] [--config CFG] [--strict]
    check_integration_skips.py --selftest [--config CFG]

`--selftest` runs the gate's own logic against synthetic pass/fail JUnit reports
(the case-(b) regression: a reintroduced missing-service silent skip MUST turn
the gate red). It needs no DB and is what the pre-commit hook runs locally.

Exit codes: 0 = gate passed; 1 = false-green detected / gate failed; 2 = usage
or input error (fail-closed — a missing/unparsable report is a failure).

SYNC: ci.yml job `integration-guard` + pre-commit hook `integration-skip-guard`.

Two configs use this same script, with opposite provisioning assumptions
(OMN-16809). `integration_skip_guard.yaml` guards the PR job, which provisions no
lane, so live-opt-in skips are legitimately optional there.
`nightly_chain_skip_guard.yaml` guards `delegation-regression-nightly.yml`, which
supplies its own lane and live-probe flag — on that job the same skip reason means
the env broke, and nothing is optional. Pass `--config` to select one.
"""

from __future__ import annotations

import argparse
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_CONFIG = Path(__file__).resolve().parent / "integration_skip_guard.yaml"


@dataclass(frozen=True)
class SkipRecord:
    """A single skipped test case extracted from a JUnit report."""

    testid: str
    reason: str


@dataclass
class GuardConfig:
    silent_skip_allowed: bool
    require_executed_min: int
    required_service_patterns: dict[str, list[re.Pattern[str]]]
    allowed_optional_patterns: list[re.Pattern[str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> GuardConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        req: dict[str, list[re.Pattern[str]]] = {}
        for svc, spec in (raw.get("required_services") or {}).items():
            pats = [
                re.compile(p, re.IGNORECASE)
                for p in (spec or {}).get("missing_skip_patterns", [])
            ]
            req[svc] = pats
        allowed = [
            re.compile(p, re.IGNORECASE)
            for p in (raw.get("allowed_optional_skip_patterns") or [])
        ]
        return cls(
            silent_skip_allowed=bool(raw.get("silent_skip_allowed", False)),
            require_executed_min=int(raw.get("require_executed_min", 1)),
            required_service_patterns=req,
            allowed_optional_patterns=allowed,
        )


@dataclass
class ReportStats:
    executed: int = 0
    skipped: list[SkipRecord] = field(default_factory=list)
    total_cases: int = 0


def _testid(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def parse_junit(paths: list[Path]) -> ReportStats:
    """Aggregate executed/skipped counts across one or more JUnit-XML files.

    A test case is "executed" when it has no <skipped> child (passed, failed, or
    errored all count as ran — the point is that the assertion body executed).
    """
    stats = ReportStats()
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"JUnit report not found: {path}")
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:  # fail-closed: an unparsable report is a failure
            raise ValueError(f"could not parse JUnit report {path}: {exc}") from exc
        for case in root.iter("testcase"):
            stats.total_cases += 1
            skipped_el = case.find("skipped")
            if skipped_el is not None:
                reason = skipped_el.get("message", "") or (skipped_el.text or "")
                stats.skipped.append(
                    SkipRecord(testid=_testid(case), reason=reason.strip())
                )
            else:
                stats.executed += 1
    return stats


def classify_skip(reason: str, cfg: GuardConfig) -> tuple[str | None, bool]:
    """Return (offending_service | None, is_allowed_optional).

    offending_service is set when the skip reason names a PROVISIONED service's
    absence (a false-green). is_allowed_optional is True when the reason matches
    a known legitimately-optional pattern.
    """
    offending: str | None = None
    for svc, pats in cfg.required_service_patterns.items():
        if any(p.search(reason) for p in pats):
            offending = svc
            break
    is_allowed = any(p.search(reason) for p in cfg.allowed_optional_patterns)
    return offending, is_allowed


def evaluate(stats: ReportStats, cfg: GuardConfig, strict: bool) -> list[str]:
    """Return a list of violation messages (empty == gate passes)."""
    violations: list[str] = []

    if cfg.silent_skip_allowed:
        # Explicit, documented opt-out. Never used in practice (config pins False).
        return violations

    for rec in stats.skipped:
        offending, is_allowed = classify_skip(rec.reason, cfg)
        if offending is not None:
            violations.append(
                f"FALSE-GREEN: integration test {rec.testid!r} SKIPPED because "
                f"provisioned service {offending!r} looked absent — reason: "
                f"{rec.reason!r}. This job provisions {offending}; this "
                f"test SHOULD have run. Wire the service env or fix the skip guard."
            )
        elif strict and not is_allowed:
            violations.append(
                f"UNCLASSIFIED-SKIP (strict): integration test {rec.testid!r} skipped "
                f"with an unrecognised reason: {rec.reason!r}. Classify it in "
                f"integration_skip_guard.yaml (required_services vs "
                f"allowed_optional_skip_patterns)."
            )

    if stats.executed < cfg.require_executed_min:
        violations.append(
            f"ZERO/UNDER-COLLECTION: only {stats.executed} integration test(s) "
            f"executed (require >= {cfg.require_executed_min}). A marker typo or a "
            f"broken selector collecting nothing is itself a false-green. "
            f"(total cases seen: {stats.total_cases})"
        )

    return violations


def run_gate(paths: list[Path], cfg: GuardConfig, strict: bool) -> int:
    stats = parse_junit(paths)
    violations = evaluate(stats, cfg, strict)
    print("=== integration silent-skip guard (OMN-14172) ===")
    print(
        f"cases={stats.total_cases} executed={stats.executed} "
        f"skipped={len(stats.skipped)} require_executed_min={cfg.require_executed_min}"
    )
    for rec in stats.skipped:
        offending, is_allowed = classify_skip(rec.reason, cfg)
        tag = (
            f"BAD[{offending}]"
            if offending
            else ("ok-optional" if is_allowed else "ok-unclassified")
        )
        print(f"  skip {tag}: {rec.testid} :: {rec.reason}")
    if violations:
        print("\nGATE FAILED — silent-skip false-green(s) detected:")
        for v in violations:
            print(f"::error::{v}")
        return 1
    print("\nGATE PASSED — no missing-service silent-skips; integration tests ran.")
    return 0


# ---------------------------------------------------------------------------
# Self-test: encodes case (a) PASS and case (b) RED with synthetic JUnit reports
# that mirror real pytest output. This is what the pre-commit hook runs locally
# (no DB required) and is the regression proof that the gate cannot silently
# stop catching the reintroduced silent-skip.
# ---------------------------------------------------------------------------

_JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" skipped="1">
 <testcase classname="tests.test_delegation_savings_tenant_id_column_omn14058"
   name="test_delegation_events_upsert_stamps_tenant_id_on_real_postgres" time="0.1"/>
 <testcase classname="tests.test_projection_delegation_tier_distribution_omn13662"
   name="test_view_classifies_not_tier_routed_against_real_postgres" time="0.1"/>
 <testcase classname="tests.node_llm.test_live_cloud_completion_omn13943" name="test_live">
   <skipped type="pytest.skip" message="live cloud delegation call; set OMN_ALLOW_LIVE_CLOUD_DELEGATION=1 to enable"/>
 </testcase>
</testsuite></testsuites>"""

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


def selftest(cfg: GuardConfig) -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        pass_xml = Path(td) / "pass.xml"
        skip_xml = Path(td) / "skip.xml"
        pass_xml.write_text(_JUNIT_PASS, encoding="utf-8")
        skip_xml.write_text(_JUNIT_SILENT_SKIP, encoding="utf-8")

        # Case (a): Postgres provisioned, integration tests ran -> gate PASSES.
        v_pass = evaluate(parse_junit([pass_xml]), cfg, strict=False)
        if v_pass:
            ok = False
            print(f"SELFTEST FAIL (case a should pass): {v_pass}")
        else:
            print("SELFTEST ok: case (a) provisioned -> PASS")

        # Case (b): missing-service silent skip reintroduced -> gate goes RED.
        v_skip = evaluate(parse_junit([skip_xml]), cfg, strict=False)
        if not v_skip:
            ok = False
            print("SELFTEST FAIL (case b should be RED): gate did not flag silent skip")
        else:
            print(
                f"SELFTEST ok: case (b) silent-skip -> RED ({len(v_skip)} violation(s))"
            )

    if not cfg.required_service_patterns:
        ok = False
        print("SELFTEST FAIL: config declares no required_services")
    if cfg.silent_skip_allowed:
        ok = False
        print("SELFTEST FAIL: silent_skip_allowed must be false")

    print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", nargs="+", type=Path, help="JUnit-XML report(s)")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail on integration skips with unclassified reasons",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = GuardConfig.load(args.config)
    except (OSError, yaml.YAMLError, re.error) as exc:
        print(f"::error::could not load guard config {args.config}: {exc}")
        return 2

    if args.selftest:
        return selftest(cfg)

    if not args.junit:
        print("::error::--junit is required (or use --selftest)")
        return 2

    try:
        return run_gate(args.junit, cfg, args.strict)
    except (FileNotFoundError, ValueError) as exc:
        print(f"::error::{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
