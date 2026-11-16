# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21927: the delegation-health verdict from counts, by rules the contract declares."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_delegation_health_compute as node_package
from omnimarket.nodes.node_delegation_health_compute.handlers.handler_delegation_health import (
    HandlerDelegationHealth,
    load_rules,
    rung_evidence,
)
from omnimarket.nodes.node_delegation_health_compute.models.model_delegation_health import (
    EnumDelegationHealthFindingKind,
    EnumDelegationHealthVerdict,
    ModelBackendCount,
    ModelDelegationHealthRequest,
    ModelDelegationHealthRules,
    ModelDelegationHealthVerdict,
    ModelWindowCounts,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
CONTRACT = NODE_DIR / "contract.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "recorded_2026_11_02T0001Z.json"
Kind = EnumDelegationHealthFindingKind
Verdict = EnumDelegationHealthVerdict


def recorded() -> dict[str, Any]:
    return dict(json.loads(FIXTURE.read_text())["request"])


def run(
    payload: dict[str, Any], rules: ModelDelegationHealthRules | None = None
) -> ModelDelegationHealthVerdict:
    return HandlerDelegationHealth(rules).handle(
        ModelDelegationHealthRequest.model_validate(payload)
    )


def with_timeouts(timed_out: int, runs: int) -> dict[str, Any]:
    """The recorded counts with `timed_out` of `runs` window runs timed out and the expected rung answering."""
    payload = recorded()
    payload["lanes_running"] = 0
    payload["outcomes"] = {
        "runs": {"h1": 0, "window": runs, "h24": runs},
        "causes": [
            {
                "cause": "timeout",
                "counts": {"h1": 0, "window": timed_out, "h24": timed_out},
            }
        ],
    }
    return payload


def contract_with(tmp_path: Path, **rules: object) -> Path:
    """A copy of the contract with verdict_rules values replaced: no code change."""
    text = CONTRACT.read_text()
    for key, value in rules.items():
        text, n = re.subn(rf"^(  {key}: ).*$", rf"\g<1>{value}", text, flags=re.M)
        assert n == 1, key
    path = tmp_path / "contract.yaml"
    path.write_text(text)
    return path


def old_rule(payload: dict[str, Any]) -> str:
    """The rule before OMN-19998: no timeout finding, and an expected rung judged by its model's count."""
    request = ModelDelegationHealthRequest.model_validate(payload)
    if request.lanes_running:
        by_model = {m.model: m for m in request.models}
        for rung in request.expected_rungs:
            model = by_model.get(rung.model)
            if model is None or model.counts.window == 0:
                return "DEGRADED"
    return "OK"


def test_verdict_types_ok_degraded_unreadable_untrusted() -> None:
    healthy = recorded()
    healthy["outcomes"]["causes"] = []
    healthy["backends"][0]["counts"]["window"] = 4
    ok = run(healthy)
    assert (ok.verdict, ok.exit_code, ok.findings) == (Verdict.OK, 0, ())
    assert ok.summary.startswith("VERDICT: OK")

    degraded = run(recorded())
    assert (degraded.verdict, degraded.exit_code) == (Verdict.DEGRADED, 1)
    assert degraded.findings
    assert all(f.reason and f.fix for f in degraded.findings)

    unreadable = run({**recorded(), "read_failure": "ssh to the lab host refused"})
    assert (unreadable.verdict, unreadable.exit_code) == (Verdict.UNREADABLE, 2)
    assert "ssh to the lab host refused" in unreadable.summary

    untrusted = run({**recorded(), "total_rows": 0})
    assert (untrusted.verdict, untrusted.exit_code) == (Verdict.UNTRUSTED, 3)
    assert untrusted.findings == ()


def test_verdict_types_models_refuse_unknown_keys_and_blank_ids() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationHealthRequest.model_validate({**recorded(), "surprise": 1})
    with pytest.raises(ValidationError):
        ModelDelegationHealthRequest.model_validate(
            {**recorded(), "correlation_id": "  "}
        )
    with pytest.raises(ValidationError):
        ModelWindowCounts.model_validate({"h1": 0, "window": 0, "h24": 0, "extra": 1})
    with pytest.raises(ValidationError):
        ModelDelegationHealthVerdict.model_validate(
            {
                "correlation_id": "x",
                "verdict": "OK",
                "exit_code": 0,
                "summary": "",
                "extra": 1,
            }
        )
    verdict = run(recorded())
    assert (
        ModelDelegationHealthVerdict.model_validate_json(verdict.model_dump_json())
        == verdict
    )


def test_verdict_types_node_is_registered_with_a_contract_and_a_handler() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["node_type"] == "compute"
    assert (NODE_DIR / "metadata.yaml").is_file()
    assert issubclass(node_package.NodeDelegationHealthCompute, HandlerDelegationHealth)
    topic = "onex.cmd.omnimarket.delegation-health-requested.v1"
    assert contract["event_bus"]["subscribe_topics"] == [topic]
    assert load_rules().timeout_share_percent == 50
    assert load_rules().timeout_min_events == 20


def test_recorded_2026_11_02t0001z_is_degraded_naming_the_timeout_share_and_the_silent_rung() -> (
    None
):
    payload = recorded()
    verdict = run(payload)
    assert verdict.verdict is Verdict.DEGRADED
    assert verdict.exit_code == 1
    by_kind = {f.kind: f for f in verdict.findings}
    assert set(by_kind) == {Kind.TIMEOUTS_DOMINATE, Kind.EXPECTED_RUNG_SILENT}
    assert by_kind[Kind.TIMEOUTS_DOMINATE].reason.startswith(
        "timeouts dominate: 132 of 172 delegations (77%) in 120m were not routed (timeout), 69 of 76 in 1h"
    )
    assert "failure_class=no_dispatcher" in by_kind[Kind.TIMEOUTS_DOMINATE].fix
    silent = by_kind[Kind.EXPECTED_RUNG_SILENT]
    assert silent.reason.startswith(
        "delegation local/Qwen3.8-27B (local rung local-coder) had 0 events in 120m while 2 lanes ran, "
        "last 2026-11-01T17:19:00Z"
    )
    assert (
        "--backend-id" not in silent.fix
    )  # the first expected rung is where an unpinned run lands
    assert "timeouts dominate" in verdict.summary
    assert "local-coder" in verdict.summary


def test_recorded_2026_11_02t0001z_the_same_counts_through_the_pre_fix_rule_read_ok() -> (
    None
):
    assert old_rule(recorded()) == "OK"
    # Positive control: the old rule is not vacuous, it reads DEGRADED once the model itself is silent.
    silent_model = recorded()
    silent_model["models"][0]["counts"]["window"] = 0
    assert old_rule(silent_model) == "DEGRADED"


def test_recorded_2026_11_02t0001z_each_blind_spot_alone_still_degrades() -> None:
    no_timeouts = recorded()
    no_timeouts["outcomes"]["causes"] = []
    assert [f.kind for f in run(no_timeouts).findings] == [Kind.EXPECTED_RUNG_SILENT]
    live_rung = recorded()
    live_rung["backends"][0]["counts"]["window"] = 5
    assert [f.kind for f in run(live_rung).findings] == [Kind.TIMEOUTS_DOMINATE]


def test_declared_threshold_share_changes_the_verdict_with_no_code_change(
    tmp_path: Path,
) -> None:
    payload = recorded()
    payload["lanes_running"] = 0
    assert run(payload).verdict is Verdict.DEGRADED
    # 132 of 172 is 76.7%: 13200 >= 172 * 76 but < 172 * 77.
    at_76 = HandlerDelegationHealth(
        load_rules(contract_with(tmp_path, timeout_share_percent=76))
    )
    at_77 = HandlerDelegationHealth(
        load_rules(contract_with(tmp_path, timeout_share_percent=77))
    )
    request = ModelDelegationHealthRequest.model_validate(payload)
    assert at_76.handle(request).verdict is Verdict.DEGRADED
    assert at_77.handle(request).verdict is Verdict.OK


def test_declared_threshold_minimum_runs_and_expected_rung_rule_are_read_from_the_contract(
    tmp_path: Path,
) -> None:
    request = ModelDelegationHealthRequest.model_validate(recorded())
    base = HandlerDelegationHealth().handle(request)
    assert {f.kind for f in base.findings} == {
        Kind.TIMEOUTS_DOMINATE,
        Kind.EXPECTED_RUNG_SILENT,
    }
    high_min = HandlerDelegationHealth(
        load_rules(contract_with(tmp_path, timeout_min_events=500))
    ).handle(request)
    assert {f.kind for f in high_min.findings} == {Kind.EXPECTED_RUNG_SILENT}
    by_model = HandlerDelegationHealth(
        load_rules(contract_with(tmp_path, expected_rung_rule="model_count"))
    ).handle(request)
    assert {f.kind for f in by_model.findings} == {Kind.TIMEOUTS_DOMINATE}


def test_declared_threshold_contract_rules_refuse_unknown_and_missing_values(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    for broken in (
        {**contract["verdict_rules"], "surprise": 1},
        {
            k: v
            for k, v in contract["verdict_rules"].items()
            if k != "timeout_share_percent"
        },
        {**contract["verdict_rules"], "expected_rung_rule": "whatever"},
        {**contract["verdict_rules"], "timeout_share_percent": 101},
    ):
        with pytest.raises(ValidationError):
            ModelDelegationHealthRules.model_validate(broken)


@pytest.mark.parametrize(
    ("timed_out", "runs", "degraded"),
    [
        (50, 100, True),
        (49, 100, False),
        (20, 20, True),
        (19, 19, False),
        (0, 172, False),
    ],
)
def test_not_judged_threshold_edges_are_inclusive(
    timed_out: int, runs: int, degraded: bool
) -> None:
    verdict = run(with_timeouts(timed_out, runs))
    assert (verdict.verdict is Verdict.DEGRADED) is degraded
    assert (Kind.TIMEOUTS_DOMINATE in {f.kind for f in verdict.findings}) is degraded


def test_not_judged_fewer_than_the_minimum_runs_never_reads_degraded_on_timeouts() -> (
    None
):
    verdict = run(with_timeouts(19, 19))
    assert verdict.verdict is Verdict.OK
    assert any("below the minimum of 20" in line for line in verdict.not_judged)
    assert "not judged" in verdict.summary


def test_not_judged_a_table_without_the_outcome_column_says_so_in_the_verdict() -> None:
    payload = with_timeouts(172, 172)
    payload["outcomes"] = None
    verdict = run(payload)
    assert verdict.verdict is Verdict.OK
    assert (
        "timeouts: the table has no terminal_failure_cause column" in verdict.not_judged
    )
    assert (
        "not judged: timeouts: the table has no terminal_failure_cause column"
        in verdict.summary
    )
    assert any(line.startswith("outcomes: not judged") for line in verdict.evidence)


def test_not_judged_expected_rungs_with_no_lanes_running() -> None:
    payload = recorded()
    payload["lanes_running"] = 0
    payload["outcomes"]["causes"] = []
    verdict = run(payload)
    assert verdict.verdict is Verdict.OK
    assert verdict.not_judged == ("expected rungs: no lanes running",)


def test_rung_findings_pin_only_and_harness_use_the_shared_evidence() -> None:
    payload = recorded()
    payload["outcomes"]["causes"] = []
    payload["backends"][0]["counts"]["window"] = 4
    payload["pin_only_rungs"] = [
        {
            "tier": "cloud",
            "backend_id": "cloud-glm",
            "provider": "glm",
            "model": "glm-5.3",
        }
    ]
    payload["backends"] += [
        {
            "backend_id": "cloud-glm",
            "counts": {"h1": 0, "window": 0, "h24": 3},
            "accepted": 0,
            "last": "2026-11-01T20:00:00Z",
        },
        {
            "backend_id": "harness:claude-glm:glm-5.3",
            "counts": {"h1": 0, "window": 0, "h24": 2},
            "accepted": 0,
            "last": "2026-11-01T21:00:00Z",
        },
    ]
    verdict = run(payload)
    assert {f.kind for f in verdict.findings} == {Kind.PIN_ONLY_RUNG, Kind.HARNESS_RUNG}
    pin = next(f for f in verdict.findings if f.kind is Kind.PIN_ONLY_RUNG)
    assert "--backend-id cloud-glm" in pin.fix
    assert any(
        "FAIL attempts=3 accepted=0 in 24h, newest 2026-11-01T20:00:00Z" in line
        for line in verdict.evidence
    )
    count = ModelBackendCount(
        backend_id="r",
        counts=ModelWindowCounts(h1=0, window=1, h24=3),
        accepted=2,
        last="2026-11-02T00:20:00Z",
        last_accept="2026-11-02T00:10:00Z",
    )
    assert (
        rung_evidence(count)
        == "PASS attempts=3 accepted=2 in 24h, newest 2026-11-02T00:20:00Z, newest accepted 2026-11-02T00:10:00Z"
    )
