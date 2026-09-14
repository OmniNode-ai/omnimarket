# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer's CLI reads the per-rule quality record (OMN-18295).

The gateway serves each declared quality rule's own verdict, the threshold it
applied, and whether it was entitled to veto. A field the client model does
not declare is a field the customer never sees, whatever the server sends -
these read models are ``extra="ignore"`` precisely so an additive server field
is not a client outage, and the same setting is why an undeclared field is
silently dropped rather than raising.

These tests cover the client half: the two read models carry the record, and
``onex cloud delegate`` prints it on EVERY terminal run. The last part is the
half no failure string can carry - ``deciding_rules=`` lives inside
``terminal_failure_reason``, which names the blocking rules only and is absent
on a run that completed.
"""

from __future__ import annotations

import typing as t
import uuid

import pytest

from omnimarket.cli.cli_cloud import _rule_evaluation_lines
from omnimarket.cloud.model_cloud_delegation import (
    ModelCloudDelegationReceipt,
    ModelCloudDelegationStatus,
    ModelCloudQualityRuleEvaluation,
)

pytestmark = pytest.mark.unit


_CONCISE_MISS: dict[str, t.Any] = {
    "rule": "concise",
    "enforcement": "scored",
    "passed": False,
    "threshold": 250,
    "threshold_unit": "words",
    "detail": "response is not concise",
}
_ACCURATE_PASS: dict[str, t.Any] = {
    "rule": "accurate",
    "enforcement": "blocking",
    "passed": True,
}


def _status(**extra: t.Any) -> ModelCloudDelegationStatus:
    payload: dict[str, t.Any] = {
        "workflow_id": str(uuid.uuid4()),
        "workflow_type": "delegation-inference",
        "status": "completed",
        "envelope_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "command_topic": "onex.cmd.omnibase-infra.delegation-requested.v1",
        "submitted_at": "2026-09-13T15:00:00Z",
        "updated_at": "2026-09-13T15:00:08Z",
    }
    payload.update(extra)
    return ModelCloudDelegationStatus.model_validate(payload)


def _receipt(**extra: t.Any) -> ModelCloudDelegationReceipt:
    payload: dict[str, t.Any] = {
        "workflow_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "workflow_type": "delegation-inference",
        "status": "completed",
        "submitted_at": "2026-09-13T15:00:00Z",
        "completed_at": "2026-09-13T15:00:08Z",
        "terminal_model_used": "glm-4.6",
        "terminal_total_tokens": 99,
        "terminal_latency_ms": 1083,
        "result_content": "ok",
        "event_count": 4,
        "projection_row_hash": "31266a6d",
        "terminal_event_hash": "9fe84da7",
        "verifier": "my-laptop",
    }
    payload.update(extra)
    return ModelCloudDelegationReceipt.model_validate(payload)


class TestTheReadModelsCarryIt:
    def test_the_status_carries_every_rule_including_the_passing_ones(self) -> None:
        carried = {
            item.rule: item
            for item in _status(
                rule_evaluations=[_ACCURATE_PASS, _CONCISE_MISS]
            ).rule_evaluations
        }
        assert set(carried) == {"accurate", "concise"}
        assert carried["accurate"].passed is True
        assert carried["concise"].threshold == 250

    def test_the_receipt_carries_them_too(self) -> None:
        receipt = _receipt(rule_evaluations=[_CONCISE_MISS])
        assert receipt.rule_evaluations[0].rule == "concise"
        assert receipt.rule_evaluations[0].enforcement == "scored"

    def test_a_gateway_that_sends_none_reads_as_empty(self) -> None:
        """An older gateway, and every run no quality gate evaluated."""
        assert _status().rule_evaluations == ()
        assert _receipt().rule_evaluations == ()

    def test_enforcement_is_an_open_string_not_a_closed_enum(self) -> None:
        """A value the server learns to emit must not break installed CLIs.

        This model ships to customer laptops upgraded on the customer's
        schedule; a closed enum would turn a server-side addition into a parse
        failure on every installed copy at once, which is what this module's
        ``extra="ignore"`` exists to prevent.
        """
        evaluation = ModelCloudQualityRuleEvaluation.model_validate(
            {"rule": "future_rule", "enforcement": "advisory", "passed": True}
        )
        assert evaluation.enforcement == "advisory"


class TestTheCliPrintsIt:
    def test_each_line_names_the_verdict_the_authority_and_the_threshold(
        self,
    ) -> None:
        lines = _rule_evaluation_lines(
            _receipt(rule_evaluations=[_CONCISE_MISS, _ACCURATE_PASS])
        )
        assert len(lines) == 2
        concise = next(line for line in lines if "concise" in line)
        assert "FAIL" in concise
        assert "[scored]" in concise
        # The 250-word limit was a literal inside the gate and appeared on no
        # receipt; a customer held to a number can now read it.
        assert "250 words" in concise
        assert "response is not concise" in concise

        accurate = next(line for line in lines if "accurate" in line)
        assert "pass" in accurate
        assert "[blocking]" in accurate

    def test_a_passing_rule_is_not_padded_with_an_invented_detail(self) -> None:
        line = _rule_evaluation_lines(_receipt(rule_evaluations=[_ACCURATE_PASS]))[0]
        assert "--" not in line

    def test_a_rule_with_no_declared_threshold_prints_no_bound(self) -> None:
        line = _rule_evaluation_lines(_receipt(rule_evaluations=[_ACCURATE_PASS]))[0]
        assert "(" not in line

    def test_a_receipt_with_no_record_prints_nothing(self) -> None:
        assert _rule_evaluation_lines(_receipt()) == ()
