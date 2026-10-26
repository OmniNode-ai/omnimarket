# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19450 AC1 and AC3 in one comparison over the bus path.

The fault suite beside this file checks each fault on its own. This test runs
all five through the same bus-path rig (real workflow handler, real inference
intent, real quality gate, a loopback provider) and compares them, reading the
canonical terminal, the one ``delegation_events`` copies its cause from:

* the wrong-key, 429 and timeout faults end in ``auth_failed``,
  ``provider_quota_exhausted`` and ``timeout``: three different causes;
* the gate-veto and truncation faults both end in ``quality_gate_refused`` and
  are told apart by typed evidence on the deciding attempt: the truncation's
  ``finish_reason`` is ``length`` with the truncated flag set, the veto's is not
  and names its vetoing rule;
* no fault reads ``provider_error`` and every fault names a cause, so all five
  are told apart by cause or deciding-attempt evidence.

Before the fix the canonical terminal named no cause for the wrong key, the
timeout and the truncation.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from tests.chains.fault_suite.test_bus_path_faults_omn19450 import (
    _FAULTS,
    _VETO_RULE,
    _run_fault,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.local_chain,
    pytest.mark.usefixtures("stub_provider_quota_reader"),
]

_Cause = EnumDelegationTerminalFailureCause


async def test_the_five_faults_are_told_apart_and_none_reads_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    runs = {
        fault: (await _run_fault(fault, monkeypatch, record_property)).canonical
        for fault in _FAULTS
    }
    causes = {fault: run.terminal_failure_cause for fault, run in runs.items()}

    # Every fault names a cause, and none is the catch-all.
    assert all(cause is not None for cause in causes.values()), causes
    assert _Cause.PROVIDER_ERROR not in causes.values(), causes

    # AC3: the three provider faults, each its own cause.
    assert causes["wrong_key"] is _Cause.AUTH_FAILED, causes
    assert causes["quota"] is _Cause.PROVIDER_QUOTA_EXHAUSTED, causes
    assert causes["timeout"] is _Cause.TIMEOUT, causes

    # AC3: the two gate faults share the gate's cause and differ in typed
    # evidence on the deciding attempt.
    assert causes["gate_veto"] is _Cause.QUALITY_GATE_REFUSED, causes
    assert causes["truncation"] is _Cause.QUALITY_GATE_REFUSED, causes
    veto = dict(runs["gate_veto"].escalation_history[-1])
    truncation = dict(runs["truncation"].escalation_history[-1])
    assert truncation.get("finish_reason") == "length", truncation
    assert truncation.get("truncated") is True, truncation
    assert veto.get("finish_reason") != "length", veto
    assert bool(veto.get("truncated")) is False, veto
    assert f"deciding_rules={_VETO_RULE}" in runs["gate_veto"].failure_reason

    # AC1, as amended: all five told apart by cause or deciding-attempt evidence.
    told_apart = {
        (cause, dict(runs[fault].escalation_history[-1]).get("truncated") is True)
        for fault, cause in causes.items()
    }
    assert len(told_apart) == len(_FAULTS), told_apart


def test_the_suite_runs_on_a_schedule_and_publishes_its_result() -> None:
    """AC2: the nightly workflow runs the fault suite on dev and publishes it.

    The scheduled run itself is read live by the contract's gh check; this pins
    what that run is: a cron trigger, a checkout of dev, the fault suite run with
    a JUnit report, a per-case job summary, and the report uploaded as an artifact.
    """
    from pathlib import Path

    import yaml

    workflow_path = (
        Path(__file__).resolve().parents[3]
        / ".github/workflows/delegation-fault-suite-nightly.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as True.
    triggers = workflow.get("on", workflow.get(True))
    assert [entry["cron"] for entry in triggers["schedule"]]
    (job,) = workflow["jobs"].values()
    steps = job["steps"]
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "dev"
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "uv run pytest tests/chains/fault_suite" in runs
    assert "--junitxml=delegation_fault_suite_junit.xml" in runs
    assert "GITHUB_STEP_SUMMARY" in runs
    upload = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )
    assert upload["with"]["path"] == "delegation_fault_suite_junit.xml"
