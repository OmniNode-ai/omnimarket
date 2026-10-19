# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local-path row registry: a row with no chain pair is named (OMN-18698).

OMN-18698 AC3: *a row with no pair is reported ABSENT, not silently green.*

The failure this closes
-----------------------
Row L10 of the local-path MVP asks for a golden chain AND an error chain for
every other row. Before this file, "is there a pair for L5?" was answered by
someone grepping the test tree, which is how the answer went stale: the
morning of 2026-09-18 recorded that no pair existed for the delegate path
when one did, and recorded nothing at all for the credential rows.

What this registry asserts, and in which direction
--------------------------------------------------
1. **Every declared pair resolves.** Each entry names a module and the
   functions inside it. A renamed, moved or deleted pair turns this red,
   naming the row -- it does not quietly become an unclaimed row.
2. **The row set is complete.** Every row of the local-path MVP appears
   exactly once. A row nobody has thought about cannot be omitted into
   silence.
3. **Absence is pinned, not merely tolerated.** :data:`EXPECTED_ABSENT` is
   the declaration of which rows have no pair TODAY. A row that gains a pair
   without being registered turns this red, and so does a row that loses one.
   That is what makes the registry a gate rather than a comment.

Why the rows are typed out here rather than read from `beta/GOAL.md`
--------------------------------------------------------------------
That file lives in a private repository this one cannot read, and CI has no
clone of it. The copy here carries each row's ticket so the two are
reconcilable by hand, and the ticket -- not the wording -- is the identity.
"""

from __future__ import annotations

import importlib

import pytest

from omnimarket.models.model_local_chain_pair_report import (
    LOCAL_ROWS,
    absence_report,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

#: The ticket that owns building the pairs this registry reports absent.
_PAIRS_TICKET = "OMN-18698"

#: The rows with no pair TODAY. Every entry is a commitment that someone
#: looked: changing this set is a decision, and leaving it stale is a red
#: test rather than a silence.
EXPECTED_ABSENT: frozenset[str] = frozenset({"L1", "L2", "L7", "L8", "L9", "R4"})


def test_every_row_appears_exactly_once() -> None:
    """A row cannot be omitted into silence, and cannot be counted twice."""
    row_ids = [row.row_id for row in LOCAL_ROWS]
    assert len(row_ids) == len(set(row_ids)), f"duplicate rows: {row_ids}"
    tickets = [row.ticket for row in LOCAL_ROWS]
    assert len(tickets) == len(set(tickets)), (
        f"two rows claim the same ticket, so one of them is mis-identified: {tickets}"
    )


def test_every_declared_pair_resolves() -> None:
    """A renamed or deleted pair turns red here, naming its row.

    Resolved by import rather than by a pytest collection sweep so the check
    is exact: a test id that no longer names a function is a broken claim,
    whatever a substring match over the tree would say.
    """
    missing: list[str] = []
    for row in LOCAL_ROWS:
        if row.module is None:
            continue
        module = importlib.import_module(row.module)
        for test_name in (*row.golden, *row.error):
            if not _resolves(module, test_name):
                missing.append(f"{row.row_id} -> {row.module}::{test_name}")
    assert not missing, (
        "the registry claims chain tests that do not exist; each line is a "
        "row whose proof has been renamed, moved or deleted:\n" + "\n".join(missing)
    )


def test_a_registered_pair_declares_both_halves() -> None:
    """A golden chain on its own is not a pair, and neither is an error chain."""
    for row in LOCAL_ROWS:
        if row.module is None:
            assert row.absent_reason, (
                f"{row.row_id} has no pair and no stated reason; an absence "
                "with no reason reads as an oversight either way"
            )
            assert not row.golden
            assert not row.error
            continue
        assert row.golden, f"{row.row_id} registers a module but no golden chain"
        assert row.error, f"{row.row_id} registers a module but no error chain"
        assert row.absent_reason is None, (
            f"{row.row_id} has a pair and an absence reason; one of the two is stale"
        )


def test_rows_without_a_pair_are_named() -> None:
    """The absent set is pinned, so a change in either direction is visible.

    This is AC3. A row that gains a pair and is not registered fails here; so
    does a row that loses one. The report names every absent row, so the
    failure output IS the list of what is left to build.
    """
    absent = frozenset(row.row_id for row in LOCAL_ROWS if not row.has_pair)
    newly_covered = EXPECTED_ABSENT - absent
    newly_absent = absent - EXPECTED_ABSENT
    assert absent == EXPECTED_ABSENT, (
        "the set of local-path rows with no chain pair has changed.\n"
        f"gained a pair (remove from EXPECTED_ABSENT): {sorted(newly_covered)}\n"
        f"lost a pair (a proof was deleted): {sorted(newly_absent)}\n"
        f"current report:\n{absence_report()}"
    )
    # The report names each one, so the absence is legible without reading
    # this file's data structures.
    report = absence_report()
    for row_id in sorted(EXPECTED_ABSENT):
        assert f"{row_id}: ABSENT" in report, (
            f"{row_id} has no pair but the report does not name it"
        )
        assert _ticket_for(row_id) in report


def test_the_pairs_this_ticket_built_are_registered() -> None:
    """OMN-18698's own deliverable, stated as a test rather than as prose.

    L11 was the stated remainder when this ticket started: its behaviour was
    not yet built. It landed mid-flight (OMN-7942, product PR #2654), so the
    pair is here rather than deferred.
    """
    covered = {row.row_id for row in LOCAL_ROWS if row.has_pair}
    expected = {"L4", "L5", "L6", "L11"}
    assert expected <= covered, (
        f"{_PAIRS_TICKET} builds the L4, L5, L6 and L11 pairs; missing "
        f"{sorted(expected - covered)}"
    )


def _resolves(module: object, test_name: str) -> bool:
    """Walk a ``TestClass::test_x`` path down from ``module``.

    A pytest node id addresses a method through its class, so a registry that
    could only see module-level functions would report every class-based pair
    absent -- which is exactly what it did on first run, for the one pair that
    already existed.
    """
    target: object = module
    for part in test_name.split("::"):
        target = getattr(target, part, None)
        if target is None:
            return False
    return callable(target)


def _ticket_for(row_id: str) -> str:
    return next(row.ticket for row in LOCAL_ROWS if row.row_id == row_id)
