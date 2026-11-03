# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20380: the triage node's Done write needs a receipt bound through binds_ac.

A merged PR is necessary and never sufficient. These tests run the REAL gate and
the REAL core rule against the verdict shapes the verifier prints, and assert
the ticket is left open in each case a receipt does not discharge the bar:

* a merged PR on a ticket whose contract binds no criterion through ``binds_ac``;
* a stale receipt: the criterion moved after the binding was accepted, whether
  the verifier already demoted the check to ``skipped`` (``CRITERIA_DRIFT``) or
  reported a ``verified`` check carrying a pin for another revision of the text;
* a skipped receipt and a ``NO_ACCEPTANCE_CHECKS`` receipt.

The positive control is the same ticket with a current, bound PASS receipt, which
closes. Without it a refusal here could be a harness that refuses everything.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_core.nodes.node_done_write_receipt_gate_compute.handler_done_write_receipt_gate import (
    criterion_pin_hash,
    live_acceptance_criteria_items,
)

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageStartCommand,
)
from tests.nodes.node_linear_triage.test_omn_20368_done_write_receipt_gate import (
    _DESCRIPTION,
    _github_with_merged_implementing_pr,
    _issue,
    _linear,
    _Probe,
    _verdict,
)

USES_REAL_DONE_WRITE_GATE = True

_AC_TEXTS = live_acceptance_criteria_items(_DESCRIPTION)


def _pins(*, ac1: str, ac2: str) -> dict[str, str]:
    return {"AC1": ac1, "AC2": ac2}


def _current_pins() -> dict[str, str]:
    return _pins(
        ac1=criterion_pin_hash(_AC_TEXTS[0]), ac2=criterion_pin_hash(_AC_TEXTS[1])
    )


def _bound_pass(*, pins: dict[str, str] | None = None) -> dict[str, Any]:
    verdict = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]})
    if pins is not None:
        for check in verdict["checks"]:
            check["ac_binding_hashes"] = dict(pins)
    return verdict


async def _sweep(verdict: dict[str, Any]) -> tuple[Any, Any, _Probe]:
    client = _linear(_issue())
    probe = _Probe(verdict)
    handler = HandlerLinearTriage(
        client=client,
        github_client=_github_with_merged_implementing_pr(),
        dod_verdict_probe=probe,
    )
    result = await handler.handle(
        ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
    )
    return result, client, probe


def _held(result: Any) -> list[Any]:
    return [
        a for a in result.actions if a.action == EnumTriageAction.HELD_NO_BOUND_RECEIPT
    ]


@pytest.mark.unit
class TestLinearTriageBoundReceiptBar:
    async def test_positive_control_current_bound_pass_receipt_closes(self) -> None:
        result, client, _ = await _sweep(_bound_pass(pins=_current_pins()))

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="uuid-1", state="Done")
        assert _held(result) == []

    async def test_merged_pr_with_no_binds_ac_is_left_open_bound_receipt_missing(
        self,
    ) -> None:
        # The OMN-20180/20181/20182 shape: checks passed, none binds a criterion.
        result, client, probe = await _sweep(_verdict(binds={"t1": [], "t2": []}))

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        client.save_comment.assert_not_called()
        held = _held(result)
        assert len(held) == 1
        assert "AC1" in held[0].evidence
        assert "AC2" in held[0].evidence
        assert probe.calls == ["OMN-820368"]

    async def test_merged_pr_binding_only_some_criteria_is_left_open_bound_receipt(
        self,
    ) -> None:
        result, client, _ = await _sweep(_verdict(binds={"t1": ["AC1"], "t2": []}))

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        held = _held(result)
        assert len(held) == 1
        assert "AC2" in held[0].evidence


@pytest.mark.unit
class TestLinearTriageStaleReceipt:
    async def test_stale_receipt_with_a_pin_for_another_revision_is_left_open(
        self,
    ) -> None:
        # The check verified, but its binding was accepted against a criterion
        # sentence the ticket no longer carries (the contract entry hash moved).
        stale = _pins(
            ac1=criterion_pin_hash(_AC_TEXTS[0]),
            ac2=criterion_pin_hash("AC2: an earlier wording of the criterion"),
        )
        result, client, _ = await _sweep(_bound_pass(pins=stale))

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        held = _held(result)
        assert len(held) == 1
        assert "AC2" in held[0].evidence

    async def test_stale_receipt_that_declines_to_name_its_revision_is_left_open(
        self,
    ) -> None:
        result, client, _ = await _sweep(
            _bound_pass(pins=_pins(ac1=criterion_pin_hash(_AC_TEXTS[0]), ac2=""))
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert len(_held(result)) == 1

    async def test_stale_receipt_the_verifier_demoted_to_skipped_is_left_open(
        self,
    ) -> None:
        # What the verifier prints once the criterion moved: the check is no
        # longer verified, it is skipped with CRITERIA_DRIFT.
        verdict = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]})
        drifted = verdict["checks"][1]
        drifted["status"] = "skipped"
        drifted["message"] = "CRITERIA_DRIFT: the criterion moved after acceptance"
        verdict["verified_count"] = 1
        result, client, _ = await _sweep(verdict)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert len(_held(result)) == 1


@pytest.mark.unit
class TestLinearTriageSkippedAndNoAcceptanceChecksReceipt:
    async def test_all_skipped_receipt_is_left_open(self) -> None:
        verdict = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]}, status="skipped")
        for check in verdict["checks"]:
            check["status"] = "skipped"
        result, client, _ = await _sweep(verdict)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert len(_held(result)) == 1

    async def test_no_acceptance_checks_receipt_is_left_open(self) -> None:
        # Every passing check is a PR-exists probe: the verifier reports
        # NO_ACCEPTANCE_CHECKS and demotes the verdict, so the status is not
        # "verified" even though the counters show passes.
        verdict = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]}, status="failed")
        verdict["verified_count"] = 2
        verdict["error_message"] = (
            "NO_ACCEPTANCE_CHECKS: OMN-820368 has no accepted acceptance-criterion "
            "falsifier, and no check in this run executed the claimed behavior."
        )
        result, client, _ = await _sweep(verdict)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert len(_held(result)) == 1

    async def test_a_skipped_check_alongside_a_verified_one_does_not_bind_its_criterion(
        self,
    ) -> None:
        verdict = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]})
        verdict["checks"][1]["status"] = "skipped"
        verdict["verified_count"] = 1
        verdict["total_checks"] = 2
        result, client, _ = await _sweep(verdict)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert len(_held(result)) == 1
