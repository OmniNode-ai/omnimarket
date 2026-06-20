#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation graded benchmark report (OMN-13369, plan P4).

Runs an offline golden-chain replay over benchmark fixture attempts. Each
attempt uses the real delegation quality-gate reducer and the shipped task-class
DoD. The report records canonical bus/projection coordinates so the evidence can
be correlated with live runs without creating any bespoke REST authority.

Usage:
    uv run python scripts/ci/run_delegation_graded_benchmark.py
    uv run python scripts/ci/run_delegation_graded_benchmark.py --json
    uv run python scripts/ci/run_delegation_graded_benchmark.py --emit /tmp/p4.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCHMARK_PATH = (
    _REPO_ROOT
    / "tests"
    / "unit"
    / "delegation"
    / "delegation_graded_benchmark_corpus.yaml"
)
_CONTRACT_PATH = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "task_class_contracts.v1.yaml"
)

_REQUIRED_CLASSES = frozenset({"easy", "medium", "hard"})


def _stable_uuid(*parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, ":".join(str(part) for part in parts))


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def _task_contracts() -> dict[str, Any]:
    return _load_yaml(_CONTRACT_PATH)["task_classes"]


def _benchmark_fixture() -> dict[str, Any]:
    return _load_yaml(_BENCHMARK_PATH)


def _dod_for(task_class: str, task_contracts: dict[str, Any]) -> dict[str, Any]:
    task = task_contracts[task_class]
    dod = task["definition_of_done"]
    if not isinstance(dod, dict):
        raise ValueError(f"task class {task_class!r} has no DoD mapping")
    return dod


def _tier_order_for(task_class: str, task_contracts: dict[str, Any]) -> list[str]:
    policy = task_contracts[task_class].get("escalation_policy", {})
    tier_order = policy.get("tier_order", ())
    return [str(tier) for tier in tier_order]


def _max_escalations_for(task_class: str, task_contracts: dict[str, Any]) -> int:
    policy = task_contracts[task_class].get("escalation_policy", {})
    return int(policy.get("max_escalations", 0))


def _score_attempt(
    *,
    case_id: str,
    task_class: str,
    attempt_index: int,
    attempt: dict[str, Any],
    dod: dict[str, Any],
    required_bar: float,
    correlation_id: UUID,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=correlation_id,
            task_type=task_class,
            llm_response_content=str(attempt["content"]),
            dod_deterministic=tuple(dod.get("deterministic", ())),
            dod_heuristic=tuple(dod.get("heuristic", ())),
        )
    )
    score = result.quality_score
    adequate = result.passed and score >= required_bar
    event_id = str(_stable_uuid("OMN-13369", case_id, attempt_index, "quality"))
    fail_category = getattr(result.fail_category, "value", result.fail_category)
    return {
        "attempt_index": attempt_index,
        "correlation_id": str(correlation_id),
        "event_id": event_id,
        "model": str(attempt["model"]),
        "tier": str(attempt["tier"]),
        "task_class": task_class,
        "score": score,
        "actual_score": score,
        "required_bar": required_bar,
        "quality_gate_passed": result.passed,
        "adequacy_passed": adequate,
        "classification": "pass" if adequate else "fail",
        "fail_category": str(fail_category),
        "fallback_recommended": result.fallback_recommended,
        "failure_reasons": list(result.failure_reasons),
        "event_refs": {
            "quality_gate_request_topic": str(fixture["quality_gate_request_topic"]),
            "quality_gate_result_topic": str(fixture["quality_gate_result_topic"]),
            "quality_gate_result_event_id": event_id,
            "projection_correlation_trace_topic": str(
                fixture["projection_topics"]["correlation_trace"]
            ),
            "projection_quality_gate_topic": str(
                fixture["projection_topics"]["quality_gate"]
            ),
            "projection_model_routing_topic": str(
                fixture["projection_topics"]["model_routing"]
            ),
        },
    }


def _score_case(
    case: dict[str, Any],
    *,
    task_contracts: dict[str, Any],
    fixture: dict[str, Any],
) -> dict[str, Any]:
    case_id = str(case["id"])
    task_class = str(case["task_class"])
    required_bar = float(case["required_bar"])
    dod = _dod_for(task_class, task_contracts)
    tier_order = _tier_order_for(task_class, task_contracts)
    max_escalations = _max_escalations_for(task_class, task_contracts)
    attempts_input = case["attempts"]

    attempts: list[dict[str, Any]] = []
    escalation_count = 0
    terminal_attempt: dict[str, Any] | None = None
    terminal_status = "failed"
    failures: list[str] = []

    if len(attempts_input) > max_escalations + 1:
        failures.append(
            f"{case_id}: fixture attempts exceed max_escalations for {task_class}"
        )

    for index, attempt in enumerate(attempts_input):
        tier = str(attempt["tier"])
        if tier_order and index < len(tier_order) and tier != tier_order[index]:
            failures.append(
                f"{case_id}: attempt {index} tier {tier!r} does not match "
                f"contract tier_order {tier_order[index]!r}"
            )

        scored = _score_attempt(
            case_id=case_id,
            task_class=task_class,
            attempt_index=index,
            attempt=attempt,
            dod=dod,
            required_bar=required_bar,
            correlation_id=_stable_uuid("OMN-13369", case_id, index),
            fixture=fixture,
        )
        attempts.append(scored)

        if scored["adequacy_passed"]:
            terminal_attempt = scored
            terminal_status = "completed"
            break

        if index < len(attempts_input) - 1 and escalation_count < max_escalations:
            escalation_count += 1
            scored["event_refs"]["escalation_event_topic"] = str(
                fixture["escalation_event_topic"]
            )
            scored["event_refs"]["escalation_event_id"] = str(
                _stable_uuid("OMN-13369", case_id, index, "escalation")
            )
            continue

        terminal_attempt = scored
        terminal_status = "failed"
        break

    if terminal_attempt is None:
        failures.append(f"{case_id}: no terminal attempt produced")
        terminal_attempt = attempts[-1]

    expected_terminal_status = str(case["expected_terminal_status"])
    expected_escalations = int(case["expected_escalations"])
    expected_terminal_tier = str(case["expected_terminal_tier"])
    expected_terminal_model = str(case["expected_terminal_model"])

    if terminal_status != expected_terminal_status:
        failures.append(
            f"{case_id}: terminal_status={terminal_status!r} "
            f"expected {expected_terminal_status!r}"
        )
    if escalation_count != expected_escalations:
        failures.append(
            f"{case_id}: escalation_count={escalation_count} "
            f"expected {expected_escalations}"
        )
    if terminal_attempt["tier"] != expected_terminal_tier:
        failures.append(
            f"{case_id}: terminal_tier={terminal_attempt['tier']!r} "
            f"expected {expected_terminal_tier!r}"
        )
    if terminal_attempt["model"] != expected_terminal_model:
        failures.append(
            f"{case_id}: terminal_model={terminal_attempt['model']!r} "
            f"expected {expected_terminal_model!r}"
        )

    terminal_topic_key = (
        "terminal_success_topic"
        if terminal_status == "completed"
        else "terminal_failure_topic"
    )

    return {
        "id": case_id,
        "benchmark_class": str(case["benchmark_class"]),
        "task_class": task_class,
        "required_bar": required_bar,
        "required_bar_source": str(case["required_bar_source"]),
        "score_source": "quality_gate_delta",
        "score_source_ticket": "OMN-12964",
        "related_evidence_ticket": "OMN-13335",
        "negative_control": bool(case.get("negative_control", False)),
        "model": terminal_attempt["model"],
        "tier": terminal_attempt["tier"],
        "score": terminal_attempt["score"],
        "actual_score": terminal_attempt["actual_score"],
        "escalation_count": escalation_count,
        "terminal_status": terminal_status,
        "classification": "pass" if terminal_status == "completed" else "fail",
        "benchmark_passed": not failures,
        "failures": failures,
        "attempts": attempts,
        "terminal_event": {
            "topic": str(fixture[terminal_topic_key]),
            "event_id": str(_stable_uuid("OMN-13369", case_id, "terminal")),
            "correlation_id": terminal_attempt["correlation_id"],
        },
        "projection_rows": {
            "correlation_trace": {
                "topic": str(fixture["projection_topics"]["correlation_trace"]),
                "correlation_id": terminal_attempt["correlation_id"],
            },
            "quality_gate": {
                "topic": str(fixture["projection_topics"]["quality_gate"]),
                "correlation_id": terminal_attempt["correlation_id"],
            },
            "model_routing": {
                "topic": str(fixture["projection_topics"]["model_routing"]),
                "correlation_id": terminal_attempt["correlation_id"],
            },
        },
    }


def _validate_report(cases: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    classes = {case["benchmark_class"] for case in cases}
    missing = sorted(_REQUIRED_CLASSES - classes)
    if missing:
        failures.append(f"missing benchmark classes: {missing}")

    negative_controls = [case for case in cases if case["negative_control"]]
    if not negative_controls:
        failures.append("missing negative controls")
    for case in negative_controls:
        if case["escalation_count"] != 0 or case["terminal_status"] != "completed":
            failures.append(
                f"{case['id']}: negative control escalated or did not complete"
            )

    if not any(
        case["escalation_count"] == 1 and case["terminal_status"] == "completed"
        for case in cases
    ):
        failures.append("no benchmark cell escalated once and passed")
    if not any(
        case["escalation_count"] >= 2 and case["terminal_status"] == "completed"
        for case in cases
    ):
        failures.append("no benchmark cell reached the ceiling and passed")
    if not any(
        case["id"] == "marker_rich_wrong_fails" and case["terminal_status"] == "failed"
        for case in cases
    ):
        failures.append("marker-rich-but-wrong control did not fail")
    if not any(
        case["id"] == "negative_correct_marker_light_passes"
        and case["terminal_status"] == "completed"
        for case in cases
    ):
        failures.append("correct-but-marker-light control did not pass")

    gradient_cells = [
        case
        for case in cases
        if case["terminal_status"] == "completed"
        and case["escalation_count"] > 0
        and case["attempts"][0]["actual_score"] < case["required_bar"]
        and case["actual_score"] >= case["required_bar"]
    ]
    if not gradient_cells:
        failures.append("no higher tier cleared a bar that the lower tier missed")

    for case in cases:
        failures.extend(case["failures"])
        if not case["benchmark_passed"]:
            failures.append(f"{case['id']}: benchmark expectation failed")

    return failures


def _build_packet() -> dict[str, Any]:
    fixture = _benchmark_fixture()
    task_contracts = _task_contracts()
    cases = [
        _score_case(case, task_contracts=task_contracts, fixture=fixture)
        for case in fixture["cases"]
    ]
    failures = _validate_report(cases)
    scores = [attempt["actual_score"] for case in cases for attempt in case["attempts"]]
    escalation_cases = [case for case in cases if case["escalation_count"] > 0]
    negative_controls = [case for case in cases if case["negative_control"]]

    return {
        "ticket": "OMN-13369",
        "gate": "delegation_graded_benchmark",
        "fixture": str(_BENCHMARK_PATH.relative_to(_REPO_ROOT)),
        "contract": str(_CONTRACT_PATH.relative_to(_REPO_ROOT)),
        "summary": {
            "n_cases": len(cases),
            "benchmark_classes": sorted({case["benchmark_class"] for case in cases}),
            "score_range": round(max(scores) - min(scores), 3) if scores else 0.0,
            "escalation_cells": len(escalation_cases),
            "negative_controls": len(negative_controls),
            "passed_cells": sum(1 for case in cases if case["benchmark_passed"]),
            "failed_terminal_cells": sum(
                1 for case in cases if case["terminal_status"] == "failed"
            ),
        },
        "event_bus": {
            "quality_gate_request_topic": str(fixture["quality_gate_request_topic"]),
            "quality_gate_result_topic": str(fixture["quality_gate_result_topic"]),
            "escalation_event_topic": str(fixture["escalation_event_topic"]),
            "terminal_success_topic": str(fixture["terminal_success_topic"]),
            "terminal_failure_topic": str(fixture["terminal_failure_topic"]),
            "projection_topics": fixture["projection_topics"],
        },
        "cases": cases,
        "failures": failures,
        "passed": not failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print benchmark evidence packet JSON"
    )
    parser.add_argument("--emit", type=Path, help="write evidence packet JSON to path")
    args = parser.parse_args(argv)

    packet = _build_packet()

    if args.emit:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(json.dumps(packet, indent=2) + "\n")
    if args.json:
        print(json.dumps(packet, indent=2))
    else:
        summary = packet["summary"]
        print(f"[delegation-graded-benchmark] OMN-13369 - {summary['n_cases']} cases")
        print(
            f"  classes={summary['benchmark_classes']} "
            f"range={summary['score_range']} "
            f"escalation_cells={summary['escalation_cells']} "
            f"negative_controls={summary['negative_controls']}"
        )
        for case in packet["cases"]:
            print(
                "  "
                f"{case['id']}: class={case['benchmark_class']} "
                f"task={case['task_class']} tier={case['tier']} "
                f"model={case['model']} score={case['actual_score']} "
                f"bar={case['required_bar']} escalations={case['escalation_count']} "
                f"terminal={case['terminal_status']} "
                f"classification={case['classification']}"
            )
        if packet["passed"]:
            print("  PASS: graded benchmark proves delegation capability gradient")
        else:
            for failure in packet["failures"]:
                print(f"  FAIL: {failure}")

    return 0 if packet["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
