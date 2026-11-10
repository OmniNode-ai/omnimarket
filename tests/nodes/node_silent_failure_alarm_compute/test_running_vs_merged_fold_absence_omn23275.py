# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host-drift fleet fold that does not exist is a declaration question (OMN-23275).

Recorded on a laptop at 2026-11-02T22:40Z (OMN-17427): every throughput tick printed
``UNKNOWN running-vs-merged | host-drift fleet fold unreadable: .../host-drift/fleet.json: [Errno 2]
No such file or directory | FIX: read the host-drift run on the operator host``. That host answered
``Could not find service "ai.omninode.host-drift"`` and is not in the manifest that declares the
unit, so the FIX named an action that does not exist where it was read. The read ported here names
the declaring hosts and whether the reading host is one of them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    HandlerRunningVsMergedAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelFleetFoldAbsence,
    ModelRunningVsMergedRequest,
)

NOW = datetime(2026, 11, 2, 22, 40, tzinfo=UTC)
UNIT = "ai.omninode.host-drift"
MANIFEST = "host_drift_manifest.yaml"
INSTALLER = "the host drift installer command"
TICKET = "OMN-17427"
OWNER = "host-a (Host-A)"


def _absence(**kw: object) -> ModelFleetFoldAbsence:
    base: dict[str, object] = {
        "path": "/x/.onex_state/host-drift/fleet.json",
        "this_host": "host-b",
        "unit": UNIT,
        "manifest": MANIFEST,
        "installer": INSTALLER,
        "ticket": TICKET,
        "owners": (OWNER,),
    }
    base.update(kw)
    return ModelFleetFoldAbsence.model_validate(base)


def _decide(absence: ModelFleetFoldAbsence | None, why: str = "[Errno 2] No such file"):
    return HandlerRunningVsMergedAlarm().handle(
        ModelRunningVsMergedRequest(
            now=NOW,
            fleet_readable=False,
            unreadable_why=why,
            fleet_absence=absence,
        )
    )


def test_fold_absence_golden_names_the_declaring_host_and_not_a_missing_file() -> None:
    result = _decide(_absence())
    assert result.state == "UNKNOWN"
    [finding] = result.findings
    assert finding.level == "unknown"
    assert finding.loop == "running-vs-merged"
    assert "[Errno 2]" not in finding.why
    assert OWNER in finding.why
    assert "host-b is not in the manifest" in finding.why
    assert (
        "declared only for host-a (Host-A) by host_drift_manifest.yaml" in finding.why
    )
    assert MANIFEST in finding.fix
    assert "on the operator host" not in finding.fix
    assert TICKET in finding.fix


def test_fold_absence_declaring_host_with_no_plist_is_told_to_install_it() -> None:
    [finding] = _decide(_absence(declared_here=True)).findings
    assert "declared for this host" in finding.why
    assert "plist is not installed" in finding.why
    assert INSTALLER in finding.fix


def test_fold_absence_installed_unit_that_wrote_no_fold_is_read_on_this_host() -> None:
    [finding] = _decide(_absence(declared_here=True, plist_installed=True)).findings
    assert "written no fold" in finding.why
    assert "tail ~/.omninode/host-drift/launchd.out.log" in finding.fix


def test_fold_absence_error_chain_nobody_declares_says_no_producer_and_names_ticket() -> (
    None
):
    [finding] = _decide(_absence(owners=())).findings
    assert "has no producer" in finding.why
    assert TICKET in finding.fix


def test_fold_absence_error_chain_other_read_error_keeps_the_file_error() -> None:
    result = _decide(None, why="/x/fleet.json: Expecting value")
    assert result.state == "UNKNOWN"
    [finding] = result.findings
    assert "unreadable: /x/fleet.json: Expecting value" in finding.why
    assert "launchctl list ai.omninode.host-drift" in finding.fix


def test_fold_absence_readable_fold_ignores_the_absence() -> None:
    result = HandlerRunningVsMergedAlarm().handle(
        ModelRunningVsMergedRequest(
            now=NOW, folded_at=NOW, fleet_absence=_absence(), items=()
        )
    )
    assert result.state == "OK"
