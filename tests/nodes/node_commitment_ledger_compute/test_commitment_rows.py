# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A commitment is a ledger row: the ask verbatim, an owner and a runnable check (OMN-23013 AC1).

The row is a STATUS row with kind=commitment, so the ledger grammar admits it unchanged. Each test builds rows only;
the failing controls inject a break (a dropped cell, a check that never passes) and show the reading fail.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_commitment_ledger_compute.handlers.handler_commitment_ledger import (
    HandlerCommitmentLedger,
)
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    EnumCommitmentStatus,
    ModelCommitmentLedgerRequest,
    ModelCommitmentLedgerResult,
)

pytestmark = pytest.mark.unit

ASK = 'Switch h053 to the NVMe | then say "done" only after the host reboots clean'
CHECK = "uv run onex host-read --host h053 --expect nvme-root"


def _handle(**fields: object) -> ModelCommitmentLedgerResult:
    return HandlerCommitmentLedger().handle(
        ModelCommitmentLedgerRequest.model_validate(fields)
    )


def _open_line(commitment_rules, now, **overrides: object) -> str:
    fields: dict[str, object] = {
        "operation": "build_open_row",
        "rules": commitment_rules,
        "now": now,
        "lane": "orchestrator-31d9",
        "ask": ASK,
        "owner": "host-bringup-1",
        "check": CHECK,
    }
    fields.update(overrides)
    result = _handle(**fields)
    assert result.row is not None, result.refusals
    return result.row


def test_open_row_carries_the_ask_verbatim_the_owner_and_the_check(
    commitment_rules, now
) -> None:
    result = _handle(
        operation="build_open_row",
        rules=commitment_rules,
        now=now,
        lane="orchestrator-31d9",
        ask=ASK,
        owner="host-bringup-1",
        check=CHECK,
        ticket="OMN-23013",
    )
    assert result.refusals == ()
    assert result.row is not None
    # The grammar admits it as a plain STATUS row: no grammar change, no new row type.
    assert grammar.row_refusals(result.row) == []
    folded = _handle(
        operation="fold",
        rules=commitment_rules,
        now=now,
        ledger_rows=[_evidence(result.row)],
    )
    (commitment,) = folded.commitments
    assert commitment.ask == ASK  # pipes and quotes survive the round trip
    assert commitment.owner == "host-bringup-1"
    assert commitment.check == CHECK
    assert commitment.ticket == "OMN-23013"
    assert commitment.status is EnumCommitmentStatus.OPEN
    assert commitment.commitment_id == f"{now:%Y-%m-%dT%H:%M:%SZ}-orchestrator-31d9"


@pytest.mark.parametrize(
    ("field", "value"),
    [("ask", "  "), ("owner", ""), ("check", ""), ("check", "uv run 'unterminated")],
)
def test_a_row_missing_what_makes_it_a_commitment_is_refused(
    commitment_rules, now, field: str, value: str
) -> None:
    result = _handle(
        operation="build_open_row",
        rules=commitment_rules,
        now=now,
        lane="orchestrator-31d9",
        **{"ask": ASK, "owner": "host-bringup-1", "check": CHECK, field: value},
    )
    assert result.row is None
    assert result.refusals, (
        "a commitment with no ask, owner or runnable check is refused"
    )


def test_a_check_with_no_runnable_argv_is_refused_not_stored(
    commitment_rules, now
) -> None:
    result = _handle(
        operation="build_open_row",
        rules=commitment_rules,
        now=now,
        lane="orchestrator-31d9",
        ask=ASK,
        owner="host-bringup-1",
        check="curl -s http://x | grep ok",
    )
    # A pipe cannot ride in a ledger cell and a check is argv with no shell, so the shell form is refused by name.
    assert result.row is None
    assert any("shell" in r or "|" in r for r in result.refusals)


def test_fold_reads_open_then_verified_then_regressed(
    commitment_rules, now, row
) -> None:
    opened = _open_line(commitment_rules, now - timedelta(hours=3))
    cid = f"{(now - timedelta(hours=3)):%Y-%m-%dT%H:%M:%SZ}-orchestrator-31d9"
    rows = [_evidence(opened)]
    assert _fold(commitment_rules, now, rows).status is EnumCommitmentStatus.OPEN
    rows.append(
        row(
            "STATUS",
            "lane=commitment-check-runner",
            "kind=commitment",
            f"commitment={cid}",
            "status=verified",
            "exit=0",
            minutes_ago=120,
        )
    )
    verified = _fold(commitment_rules, now, rows)
    assert verified.status is EnumCommitmentStatus.VERIFIED
    assert verified.last_exit == 0
    assert verified.last_checked_at is not None
    rows.append(
        row(
            "STATUS",
            "lane=commitment-check-runner",
            "kind=commitment",
            f"commitment={cid}",
            "status=regressed",
            "exit=1",
            minutes_ago=30,
        )
    )
    regressed = _fold(commitment_rules, now, rows)
    assert regressed.status is EnumCommitmentStatus.REGRESSED
    assert regressed.last_exit == 1
    assert regressed.regressions == 1


def test_injected_break_a_fold_that_ignores_the_result_rows_leaves_it_open(
    commitment_rules, now, row
) -> None:
    """The failing control of the fold: the same rows read through a rule that never reads a result row stay open."""
    opened = _open_line(commitment_rules, now - timedelta(hours=3))
    cid = f"{(now - timedelta(hours=3)):%Y-%m-%dT%H:%M:%SZ}-orchestrator-31d9"
    result_row = row(
        "STATUS",
        "lane=commitment-check-runner",
        "kind=commitment",
        f"commitment={cid}",
        "status=verified",
        "exit=0",
        minutes_ago=10,
    )
    broken = commitment_rules.model_copy(update={"kind_value": "not-a-commitment"})
    folded = _handle(
        operation="fold",
        rules=broken,
        now=now,
        ledger_rows=[_evidence(opened), result_row],
    )
    assert folded.commitments == ()  # nothing reads: the break is visible
    assert _fold(commitment_rules, now, [_evidence(opened), result_row]).status is (
        EnumCommitmentStatus.VERIFIED
    )


def test_malformed_and_orphan_rows_are_named_never_dropped(
    commitment_rules, now, row
) -> None:
    no_check = row(
        "STATUS",
        "lane=orchestrator-31d9",
        "kind=commitment",
        "commitment=2026-11-07T15:00:00Z-orchestrator-31d9",
        "status=open",
        'ask="an ask with no check"',
        "owner=someone",
        minutes_ago=60,
    )
    orphan = row(
        "STATUS",
        "lane=commitment-check-runner",
        "kind=commitment",
        "commitment=2026-11-01T00:00:00Z-ghost",
        "status=verified",
        "exit=0",
        minutes_ago=5,
    )
    result = _handle(
        operation="fold",
        rules=commitment_rules,
        now=now,
        ledger_rows=[no_check, orphan],
    )
    assert result.commitments == ()
    assert len(result.malformed) == 2
    assert any("check" in m for m in result.malformed)
    assert any("ghost" in m for m in result.malformed)


def test_a_withdrawn_commitment_stays_withdrawn_whatever_reading_follows(
    commitment_rules, now, row
) -> None:
    opened = _open_line(commitment_rules, now - timedelta(hours=3))
    cid = f"{(now - timedelta(hours=3)):%Y-%m-%dT%H:%M:%SZ}-orchestrator-31d9"
    withdraw = _handle(
        operation="build_withdraw_row",
        rules=commitment_rules,
        now=now - timedelta(hours=1),
        lane="orchestrator-31d9",
        commitment_id=cid,
    )
    assert withdraw.row is not None, withdraw.refusals
    assert grammar.row_refusals(withdraw.row) == []
    late_reading = row(
        "STATUS",
        "lane=commitment-check-runner",
        "kind=commitment",
        f"commitment={cid}",
        "status=verified",
        "exit=0",
        minutes_ago=5,
    )
    folded = _fold(
        commitment_rules,
        now,
        [_evidence(opened), _evidence(withdraw.row), late_reading],
    )
    assert folded.status is EnumCommitmentStatus.WITHDRAWN


def test_a_withdrawal_naming_no_commitment_is_refused(commitment_rules, now) -> None:
    result = _handle(
        operation="build_withdraw_row",
        rules=commitment_rules,
        now=now,
        lane="orchestrator-31d9",
        commitment_id="not-an-id",
    )
    assert result.row is None
    assert result.refusals


def _evidence(line: str):
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        evidence_row_from_text,
    )

    parsed = evidence_row_from_text(line)
    assert parsed is not None, line
    return parsed


def _fold(commitment_rules, now, rows):
    result = _handle(
        operation="fold", rules=commitment_rules, now=now, ledger_rows=rows
    )
    (commitment,) = result.commitments
    return commitment
