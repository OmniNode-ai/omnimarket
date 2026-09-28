# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19121 — the Done-flip gate reads an attempt-scoped supersede record.

Since OMN-19050 a re-executed check files its correction as
``<check>.supersede.<pr>.<NNNN>.yaml``. Three readers of that shape were
updated at the time. This one was not, and it is a gate: it decides which
receipt the pre-Linear-Done ``DurableEvidenceGate`` reads.

The chain used throughout is the real one from OMN-18868 / omnimarket#2751,
which is what the defect was measured on: a base ``PENDING`` receipt, a
``FAIL`` at head ``bab99887``, and the correcting ``PASS`` at head
``b5c6f7a4``. Before the fix this gate selected the FAIL, because the
correcting record's dotted token produced no ordinal and it sorted below the
record it corrects. Check 2 then keeps only ``status == "PASS"`` receipts, so
the key contributed nothing and the flip reported no PASS receipt binding the
PR — while the binding resolver, ``resolve_supersession``, read the PASS.

The differentiator is the fourth case: a PASS re-filed at the FAIL's OWN
``commit_sha``. Widening the ordering alone would make this gate SELECT that
record, moving it from disagreeing with the resolver in the closed direction
to disagreeing in the open one. The commit-identity guard is what keeps the
two answers the same, and the cases below fail for that specific reason
rather than because the record is invisible.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    _supersede_sequence,
    apply_supersessions,
)

_TICKET = "OMN-18868"
_ITEM = "dod-occ-diff-derived-behavior-proof-pr-2751"
_CHECK = "test_passes"
_PR = 2751

# The two real heads of the measured chain.
_FAIL_HEAD = "bab9988752f031628bf02fbecae852bf6fa0acee"
_PASS_HEAD = "b5c6f7a483230fb219316717afc6badb0c3c6cfe"

_BASE_NAME = f"{_CHECK}.yaml"
_FAIL_NAME = f"{_CHECK}.supersede.{_PR}.yaml"
_PASS_NAME = f"{_CHECK}.supersede.{_PR}.0002.yaml"
_RELAUNDERED_NAME = f"{_CHECK}.supersede.{_PR}.0003.yaml"


def _base() -> dict[str, object]:
    """The minted placeholder the runner's records correct."""
    return {
        "__source_name__": _BASE_NAME,
        "ticket_id": _TICKET,
        "evidence_item_id": _ITEM,
        "check_type": _CHECK,
        "status": "PENDING",
        "commit_sha": _FAIL_HEAD,
        "pr_number": _PR,
    }


def _record(
    source_name: str, status: str, commit_sha: str, created_at: str
) -> dict[str, object]:
    """One ``ModelReceiptSupersession``-shaped payload as the loader yields it."""
    return {
        "__source_name__": source_name,
        "ticket_id": _TICKET,
        "evidence_item_id": _ITEM,
        "check_type": _CHECK,
        "supersedes": f"drift/dod_receipts/{_TICKET}/{_ITEM}/{_BASE_NAME}",
        "created_at": created_at,
        "tombstone": False,
        "replacement": {
            "ticket_id": _TICKET,
            "evidence_item_id": _ITEM,
            "check_type": _CHECK,
            "status": status,
            "commit_sha": commit_sha,
            "pr_number": _PR,
        },
    }


def _fail_record() -> dict[str, object]:
    return _record(_FAIL_NAME, "FAIL", _FAIL_HEAD, "2026-09-21T15:40:50Z")


def _pass_record() -> dict[str, object]:
    return _record(_PASS_NAME, "PASS", _PASS_HEAD, "2026-09-21T20:57:56Z")


def _relaundered_record() -> dict[str, object]:
    """The differentiator: the same PASS observation, re-filed at the FAIL's head."""
    return _record(_RELAUNDERED_NAME, "PASS", _FAIL_HEAD, "2026-09-21T21:30:00Z")


def _active(receipts: list[dict[str, object]]) -> dict[str, object]:
    resolved = apply_supersessions(receipts)
    assert len(resolved) == 1, resolved
    return resolved[0]


@pytest.mark.unit
def test_attempt_scoped_token_orders_above_the_record_it_corrects() -> None:
    """The ordering primitive, alone: a dotted token is visible and ranks higher.

    This is the half that was missing outright. ``_supersede_sequence`` mirrors
    the core resolver's ``_sequence_key``: a bare token keys to a 1-tuple and
    compares exactly as ``int(token)`` did, so existing chains are unmoved.
    """
    assert _supersede_sequence("2751") == (2751,)
    assert _supersede_sequence("2751.0002") == (2751, 2)
    assert _supersede_sequence("2751") < _supersede_sequence("2751.0002")
    # Not dotted-numeric: kept out of the sequence tiebreak entirely, exactly
    # as the prior isdigit filter did, and never compared against a tuple.
    assert _supersede_sequence("15459-x") is None
    assert _supersede_sequence("") is None
    assert _supersede_sequence(None) is None


@pytest.mark.unit
def test_live_chain_resolves_to_the_correcting_pass() -> None:
    """The measured defect. Pre-fix this returned the FAIL at ``bab99887``."""
    active = _active([_base(), _fail_record(), _pass_record()])
    assert active["status"] == "PASS"
    assert active["commit_sha"] == _PASS_HEAD


@pytest.mark.unit
def test_a_pass_refiled_at_the_failing_head_does_not_clear_the_fail() -> None:
    """The differentiator, and the reason the guard rides with the widening.

    Ordering alone selects ``0003``, because it is both later and higher. The
    guard returns the FAIL instead, because the replacement carries the FAIL's
    own ``commit_sha`` and is therefore the same observation restated rather
    than an independent one. Without the guard this gate would answer PASS
    where ``resolve_supersession`` answers FAIL — an OPEN disagreement, which
    is worse than the closed one this ticket fixes.
    """
    # Guard against a false green: the record IS visible and IS ranked above
    # the FAIL, so what follows is the guard's answer, not blindness.
    assert _supersede_sequence(f"{_PR}.0003") > _supersede_sequence(str(_PR))

    active = _active([_base(), _fail_record(), _relaundered_record()])
    assert active["status"] == "FAIL"
    assert active["commit_sha"] == _FAIL_HEAD


@pytest.mark.unit
def test_a_relaundered_pass_also_regresses_an_established_pass() -> None:
    """Full chain, matching the resolver's answer for the same three records.

    A genuine PASS already stands at ``b5c6f7a4`` when a PASS is re-filed at
    the old failing head. The guard walks back past the intervening PASS to the
    latest FAIL and finds the winner restating it, so the FAIL stands. This is
    the resolver's behaviour, asserted here so the two cannot drift apart.
    """
    active = _active([_base(), _fail_record(), _pass_record(), _relaundered_record()])
    assert active["status"] == "FAIL"
    assert active["commit_sha"] == _FAIL_HEAD


@pytest.mark.unit
def test_a_pass_at_a_third_head_is_an_independent_observation() -> None:
    """The guard bites on identity, not on lateness.

    A re-filed PASS whose head differs from the FAIL's is a real re-run and
    supersedes it. Without this case the guard could be satisfied by refusing
    every later PASS, which would re-latch the very defect OMN-19050 fixed.
    """
    third_head = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
    independent = _record(_RELAUNDERED_NAME, "PASS", third_head, "2026-09-21T21:30:00Z")
    active = _active([_base(), _fail_record(), independent])
    assert active["status"] == "PASS"
    assert active["commit_sha"] == third_head


@pytest.mark.unit
def test_a_bare_single_attempt_chain_is_unchanged() -> None:
    """Backward compatibility: the overwhelming majority of chains on disk.

    One bare ``.supersede.<pr>.yaml`` over a base receipt resolved to that
    record before this change and must still resolve to it. A chain that has
    never used a dotted token must not move.
    """
    active = _active([_base(), _fail_record()])
    assert active["status"] == "FAIL"
    assert active["commit_sha"] == _FAIL_HEAD


@pytest.mark.unit
def test_a_non_numeric_token_still_loses_to_a_numbered_record() -> None:
    """A legacy hand-token record never outranks a real ordinal, and never raises.

    The pre-fix ordering compared ints; this one compares tuples. A token that
    is neither must not reach the comparison, or the gate raises TypeError on a
    corpus that already contains such names.
    """
    legacy = _record(
        f"{_CHECK}.supersede.15459-x.yaml",
        "PASS",
        _PASS_HEAD,
        "2026-09-22T00:00:00Z",
    )
    active = _active([_base(), legacy, _fail_record()])
    assert active["status"] == "FAIL"
    assert active["commit_sha"] == _FAIL_HEAD


@pytest.mark.unit
def test_a_tombstone_still_drops_the_key_whatever_the_token() -> None:
    """Key-wide invalidation is not a status question and the guard must not touch it."""
    tombstone = {
        "__source_name__": _RELAUNDERED_NAME,
        "ticket_id": _TICKET,
        "evidence_item_id": _ITEM,
        "check_type": _CHECK,
        "supersedes": f"drift/dod_receipts/{_TICKET}/{_ITEM}/{_BASE_NAME}",
        "created_at": "2026-09-21T21:30:00Z",
        "tombstone": True,
    }
    assert apply_supersessions([_base(), _fail_record(), tombstone]) == []
