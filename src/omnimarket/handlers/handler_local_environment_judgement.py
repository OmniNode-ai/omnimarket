# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure verdicts and open alerts for the local environment's services.

Shared by ``node_local_alert_compute`` (whose contract declares the rules) and
``node_projection_local_environment`` (which judges every row of a tenant's mode
on each report it stores), so neither node imports the other.

A service's verdict is its last observed state, unless its last report is older
than the freshness deadline: then it is STALE, because the observer no longer
vouches for any state. A verdict opens the alert the rules give it, if any. No
clock is read: the observation time is passed in.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path

import yaml

from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.models.model_local_service_health import (
    ModelLocalAlert,
    ModelLocalAlertRules,
    ModelLocalServiceHealth,
    ModelLocalServiceVerdict,
)

_STATE_VERDICT = {
    EnumLocalServiceState.RUNNING: EnumLocalServiceVerdict.RUNNING,
    # A one-shot service that finished cleanly did what it is for.
    EnumLocalServiceState.EXITED_OK: EnumLocalServiceVerdict.RUNNING,
    EnumLocalServiceState.DOWN: EnumLocalServiceVerdict.DOWN,
    EnumLocalServiceState.UNHEALTHY: EnumLocalServiceVerdict.UNHEALTHY,
}


def load_local_alert_rules(contract_path: Path | None = None) -> ModelLocalAlertRules:
    """Read the rules and the freshness deadline from the alert compute's contract."""
    text = (
        contract_path.read_text()
        if contract_path is not None
        else files("omnimarket.nodes.node_local_alert_compute")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return ModelLocalAlertRules.model_validate(
        yaml.safe_load(text)["config"]["local_alert"]
    )


def _judge(
    service: ModelLocalServiceHealth, observed_at: datetime, deadline: timedelta
) -> tuple[EnumLocalServiceVerdict, str, datetime]:
    silence = observed_at - service.last_seen
    if silence > deadline:
        return (
            EnumLocalServiceVerdict.STALE,
            f"no report for {int(silence.total_seconds())} s",
            service.last_seen + deadline,
        )
    return _STATE_VERDICT[service.observed_state], service.reason, service.last_change


def judge_local_services(
    services: tuple[ModelLocalServiceHealth, ...],
    observed_at: datetime,
    rules: ModelLocalAlertRules,
) -> tuple[tuple[ModelLocalServiceVerdict, ...], tuple[ModelLocalAlert, ...]]:
    """A verdict per service, in the given order, and the alerts they open."""
    deadline = timedelta(seconds=rules.freshness_deadline_seconds)
    rule_by_verdict = {rule.verdict: rule for rule in rules.rules}
    verdicts: list[ModelLocalServiceVerdict] = []
    alerts: list[ModelLocalAlert] = []
    for service in services:
        verdict, reason, since = _judge(service, observed_at, deadline)
        rule = rule_by_verdict.get(verdict)
        verdicts.append(
            ModelLocalServiceVerdict(
                tenant_id=service.tenant_id,
                mode=service.mode,
                service=service.service,
                verdict=verdict,
                reason=reason,
                alert_code=None if rule is None else rule.code,
                alert_severity=None if rule is None else rule.severity,
                alert_since=None if rule is None else since,
            )
        )
        if rule is not None:
            alerts.append(
                ModelLocalAlert(
                    tenant_id=service.tenant_id,
                    mode=service.mode,
                    service=service.service,
                    code=rule.code,
                    severity=rule.severity,
                    since=since,
                )
            )
    return tuple(verdicts), tuple(alerts)


__all__ = ["judge_local_services", "load_local_alert_rules"]
