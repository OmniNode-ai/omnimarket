# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24980: delegation health is DEGRADED when no delegated run was answered locally while a local endpoint is up.

Read on 2026-11-16: local models answered 2 lanes of the day's delegated runs while GLM took 67 runs on 38 lanes,
and the 11-11 proof of local-first accepted runs a hosted model had served, so nothing caught that local never
answered. The window and the minimum run count are the contract's `verdict_rules`, not code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import omnimarket.nodes.node_delegation_health_compute as node_package
from omnimarket.nodes.node_delegation_health_compute.handlers.handler_delegation_health import (
    HandlerDelegationHealth,
    load_rules,
)
from omnimarket.nodes.node_delegation_health_compute.models.model_delegation_health import (
    EnumDelegationHealthFindingKind,
    EnumDelegationHealthVerdict,
    ModelDelegationHealthRequest,
    ModelDelegationHealthVerdict,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(node_package.__file__).parent / "contract.yaml"
Kind = EnumDelegationHealthFindingKind
ENDPOINT = "local-coder at the first lab model host answered /v1/models"


def request(**local_share: Any) -> ModelDelegationHealthRequest:
    share = {
        "window_minutes": 360,
        "delegated_runs": 40,
        "local_runs": 0,
        "local_endpoints_up": (ENDPOINT,),
    }
    share.update(local_share)
    return ModelDelegationHealthRequest.model_validate(
        {
            "correlation_id": "c1",
            "quiet_minutes": 360,
            "total_rows": 100,
            "local_share": share,
        }
    )


def judge(
    req: ModelDelegationHealthRequest, rules=None
) -> ModelDelegationHealthVerdict:  # type: ignore[no-untyped-def]
    return HandlerDelegationHealth(rules).handle(req)


def local_not_judged(verdict: ModelDelegationHealthVerdict) -> list[str]:
    return [line for line in verdict.not_judged if line.startswith("local share")]


def kinds(verdict: ModelDelegationHealthVerdict) -> list[Kind]:
    return [f.kind for f in verdict.findings]


def test_zero_local_runs_with_a_local_endpoint_up_is_degraded() -> None:
    verdict = judge(request())
    assert verdict.verdict is EnumDelegationHealthVerdict.DEGRADED
    assert kinds(verdict) == [Kind.LOCAL_SHARE_ZERO]
    assert "40 delegated runs in 360m" in verdict.findings[0].reason
    assert ENDPOINT in verdict.findings[0].reason
    assert "--backend-id local-heavy-reasoning" in verdict.findings[0].fix


def test_one_local_run_is_not_degraded() -> None:
    verdict = judge(request(local_runs=1))
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert kinds(verdict) == []


def test_no_local_endpoint_up_is_not_judged_never_degraded() -> None:
    verdict = judge(request(local_endpoints_up=()))
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert local_not_judged(verdict) == [
        "local share: no declared local endpoint answered"
    ]


def test_too_few_runs_is_not_judged() -> None:
    verdict = judge(request(delegated_runs=4))
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert "below the minimum of 5" in local_not_judged(verdict)[0]


def test_a_window_shorter_than_the_rule_is_not_judged() -> None:
    verdict = judge(request(window_minutes=60))
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert "below the 360m window" in local_not_judged(verdict)[0]


def test_no_counts_read_is_not_judged() -> None:
    req = ModelDelegationHealthRequest.model_validate(
        {"correlation_id": "c1", "quiet_minutes": 360, "total_rows": 100}
    )
    verdict = judge(req)
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert local_not_judged(verdict) == [
        "local share: the caller read no local share counts"
    ]


def test_local_runs_above_delegated_runs_is_refused() -> None:
    with pytest.raises(ValidationError):
        request(delegated_runs=3, local_runs=4)


def test_the_window_and_minimum_are_contract_values_not_code(tmp_path: Path) -> None:
    text = CONTRACT.read_text()
    for key, value in (
        ("local_share_window_minutes", 30),
        ("local_share_min_runs", 50),
    ):
        text, n = re.subn(rf"^(  {key}: ).*$", rf"\g<1>{value}", text, flags=re.M)
        assert n == 1, key
    path = tmp_path / "contract.yaml"
    path.write_text(text)
    rules = load_rules(path)
    # 40 runs is now below the minimum of 50 that the changed contract declares.
    verdict = judge(request(window_minutes=30), rules)
    assert verdict.verdict is EnumDelegationHealthVerdict.OK
    assert "below the minimum of 50" in local_not_judged(verdict)[0]
    # and the packaged contract still judges the same counts DEGRADED.
    assert (
        judge(request(window_minutes=360)).verdict
        is EnumDelegationHealthVerdict.DEGRADED
    )


def test_the_contract_declares_the_six_hour_window() -> None:
    rules = load_rules()
    assert rules.local_share_window_minutes == 360
    assert Kind.LOCAL_SHARE_ZERO in rules.fixes
