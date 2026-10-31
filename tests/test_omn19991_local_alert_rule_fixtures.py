# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19991: every rule in the local alert compute's contract has a positive and a negative fixture.

The rules are read from the contract and the fixtures from the data file the
compute node ships beside it, keyed by rule code, so the parametrisation grows with the contract: a rule added to
the contract with no fixture fails here, rather than going untested because a
hand-written list of indexes did not grow with it.

Failure modes each test is written against:

* a contract rule has no positive or no negative fixture, and the suite passes;
* a positive fixture opens no alert, or another rule's alert;
* a negative fixture opens the rule's alert;
* the coverage check reads its own copy of the rules rather than the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.models.model_local_service_health import (
    ModelLocalAlertRule,
    ModelLocalAlertRules,
    ModelLocalServiceHealth,
)
from omnimarket.nodes.node_local_alert_compute.handlers.handler_local_alert import (
    HandlerLocalAlert,
    load_local_alert_rules,
)
from omnimarket.nodes.node_local_alert_compute.models import ModelLocalAlertRequest

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
TENANT = "11111111-1111-4111-8111-111111111111"
CONTRACT_RULES = load_local_alert_rules()


def _fixtures() -> dict[str, dict[str, dict[str, Any]]]:
    text = (
        files("omnimarket.nodes.node_local_alert_compute")
        .joinpath("rule_fixtures.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, dict[str, dict[str, Any]]] = yaml.safe_load(text)
    return loaded


def rules_missing_fixtures(
    rules: ModelLocalAlertRules, fixtures: dict[str, dict[str, dict[str, Any]]]
) -> list[str]:
    """Each rule code lacking a positive or a negative fixture."""
    return [
        rule.code
        for rule in rules.rules
        if not {"positive", "negative"} <= set(fixtures.get(rule.code, {}))
    ]


def _alert_codes(fixture: dict[str, Any], rules: ModelLocalAlertRules) -> list[str]:
    service = ModelLocalServiceHealth(
        tenant_id=TENANT,
        mode=EnumLocalEnvironmentMode.COMPOSE,
        service="postgres",
        observed_state=EnumLocalServiceState(fixture["observed_state"]),
        reason="fixture",
        last_seen=T0,
        last_change=T0,
    )
    result = HandlerLocalAlert().handle(
        ModelLocalAlertRequest(
            services=(service,),
            observed_at=T0 + timedelta(seconds=int(fixture["silence_seconds"])),
            rules=rules,
        )
    )
    return [alert.code for alert in result.alerts]


def test_the_contract_declares_rules() -> None:
    # Positive control: an empty rule list would make every check below vacuous.
    assert len(CONTRACT_RULES.rules) >= 3


def test_every_contract_rule_has_a_positive_and_a_negative_fixture() -> None:
    assert rules_missing_fixtures(CONTRACT_RULES, _fixtures()) == []


def test_a_rule_without_a_fixture_is_reported_missing() -> None:
    extended = ModelLocalAlertRules(
        freshness_deadline_seconds=CONTRACT_RULES.freshness_deadline_seconds,
        rules=(
            *(r for r in CONTRACT_RULES.rules if r.code != "SERVICE_DOWN"),
            ModelLocalAlertRule(
                code="SERVICE_GONE",
                severity=EnumLocalAlertSeverity.CRITICAL,
                verdict=EnumLocalServiceVerdict.DOWN,
            ),
        ),
    )
    assert rules_missing_fixtures(extended, _fixtures()) == ["SERVICE_GONE"]
    half = {**_fixtures(), "SERVICE_DOWN": {"positive": {}}}
    assert rules_missing_fixtures(CONTRACT_RULES, half) == ["SERVICE_DOWN"]


@pytest.mark.parametrize("rule", CONTRACT_RULES.rules, ids=lambda r: r.code)
def test_the_positive_fixture_opens_exactly_its_rule(
    rule: ModelLocalAlertRule,
) -> None:
    assert _alert_codes(_fixtures()[rule.code]["positive"], CONTRACT_RULES) == [
        rule.code
    ]


@pytest.mark.parametrize("rule", CONTRACT_RULES.rules, ids=lambda r: r.code)
def test_the_negative_fixture_does_not_open_its_rule(
    rule: ModelLocalAlertRule,
) -> None:
    assert rule.code not in _alert_codes(
        _fixtures()[rule.code]["negative"], CONTRACT_RULES
    )
