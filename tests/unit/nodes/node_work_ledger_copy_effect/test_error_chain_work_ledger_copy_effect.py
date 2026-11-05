# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every injected break ends in its own typed outcome, is reported, and prunes nothing."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_ledger_copy_effect.handlers import HandlerWorkLedgerCopy
from omnimarket.nodes.node_work_ledger_copy_effect.models import EnumCopyOutcome
from tests.unit.nodes.node_work_ledger_copy_effect.support import (
    FakeDrill,
    FakeReporter,
    FakeSource,
    FakeStore,
    request,
)

pytestmark = pytest.mark.unit


def _run(
    *,
    store: FakeStore | None = None,
    source: FakeSource | None = None,
    **drill_break: bool,
) -> tuple[EnumCopyOutcome, FakeStore, FakeSource, FakeReporter, str]:
    store = store or FakeStore()
    source = source or FakeSource()
    reporter = FakeReporter()
    handler = HandlerWorkLedgerCopy(
        source=source,
        store=store,
        drill=FakeDrill(store, **drill_break),
        reporter=reporter,
        keep=3,
        min_free_bytes=1000,
    )
    result = handler.handle(request())
    assert reporter.reports == [result]
    return result.outcome, store, source, reporter, result.detail


def test_low_space_refuses_before_any_dump() -> None:
    outcome, store, source, _, _ = _run(store=FakeStore(free=10))
    assert outcome is EnumCopyOutcome.REFUSED_LOW_SPACE
    assert source.dumped == 0
    assert store.placed == {}


def test_dump_failure_places_nothing() -> None:
    outcome, store, _, _, detail = _run(source=FakeSource(fail_dump=True))
    assert outcome is EnumCopyOutcome.DUMP_FAILED
    assert "connection refused" in detail
    assert store.placed == {}


def test_a_byte_changed_in_transit_is_a_transfer_mismatch_and_prunes_nothing() -> None:
    outcome, store, _, _, detail = _run(store=FakeStore(corrupt=True))
    assert outcome is EnumCopyOutcome.TRANSFER_MISMATCH
    assert "projection.dump" in detail
    assert store.pruned_keep is None


def test_drill_crash_is_drill_failed() -> None:
    outcome, store, _, _, _ = _run(crash=True)
    assert outcome is EnumCopyOutcome.DRILL_FAILED
    assert store.pruned_keep is None


def test_named_break_lose_last_row_fails_the_row_count_and_last_row_match() -> None:
    outcome, store, _, _, detail = _run(lose_last_row=True)
    assert outcome is EnumCopyOutcome.DRILL_MISMATCH
    assert "row count 99 against 100" in detail
    assert "last row differs" in detail
    assert store.pruned_keep is None


def test_named_break_drop_file_tail_fails_the_file_match() -> None:
    outcome, _, _, _, detail = _run(drop_file_tail=True)
    assert outcome is EnumCopyOutcome.DRILL_MISMATCH
    assert "ledger file" in detail


def test_the_unbroken_run_passes_so_the_breaks_are_what_fail_it() -> None:
    outcome, *_ = _run()
    assert outcome is EnumCopyOutcome.COPIED_VERIFIED
