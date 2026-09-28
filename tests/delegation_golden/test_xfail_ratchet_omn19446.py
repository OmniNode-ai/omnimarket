# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The corpus's xfail set is a shrink-only ratchet (OMN-19446).

Three of nine Layer-2 cases (I2, I4, I8) carried an `xfail` block, which keeps
the nightly green no matter what the live lane does on those cases. Nothing
stopped a fourth case picking up an undiscussed xfail and quietly widening the
set the nightly cannot fail on. This module owns the frozen baseline (naming a
ticket per case, mirroring the ``handler_dispatch_entrypoint`` shrink-only
baseline pattern already used in this repo) and its enforcement:

  * a corpus case xfailed but NOT in the baseline -> FAIL (no new instances,
    ever, without a reviewed baseline edit in the same PR);
  * a baseline entry whose case is no longer xfailed in the corpus -> FAIL
    (the baseline must shrink with the corpus, never lag stale).

The positive controls below are the reason a green here means anything: a gate
that cannot be made to fail has not passed, it has not run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.delegation_golden.corpus_loader import ModelCorpus, load_corpus
from tests.delegation_golden.xfail_ratchet import (
    DEFAULT_BASELINE_PATH,
    load_baseline,
    ratchet_violations,
)


def _corpus_with_xfail_ids() -> tuple[ModelCorpus, frozenset[str]]:
    corpus = load_corpus()
    xfail_ids = frozenset(c.id for c in corpus.cases if c.xfail is not None)
    return corpus, xfail_ids


def test_baseline_file_exists_and_is_nonempty() -> None:
    assert DEFAULT_BASELINE_PATH.is_file(), (
        f"{DEFAULT_BASELINE_PATH} is missing -- the shrink-only xfail baseline "
        "must be a committed file, not derived from the corpus it audits."
    )
    baseline = load_baseline(DEFAULT_BASELINE_PATH)
    assert baseline, "baseline loaded empty -- positive control below covers a real set"


def test_every_live_xfail_case_is_in_the_baseline() -> None:
    """No corpus case may carry `xfail` unless the baseline already names it."""
    _corpus, xfail_ids = _corpus_with_xfail_ids()
    baseline = load_baseline(DEFAULT_BASELINE_PATH)
    violations, _stale = ratchet_violations(xfail_ids, baseline)
    assert not violations, (
        "corpus case(s) carry xfail with no matching baseline entry (new xfail "
        f"instances are refused): {sorted(violations)}. Either fix the case or "
        f"add it to {DEFAULT_BASELINE_PATH} naming a tracking ticket, in the "
        "same PR, with a reviewer's sign-off -- the baseline is not self-serve."
    )


def test_baseline_has_no_stale_entries() -> None:
    """A baseline entry whose case no longer carries xfail must be removed."""
    _corpus, xfail_ids = _corpus_with_xfail_ids()
    baseline = load_baseline(DEFAULT_BASELINE_PATH)
    _violations, stale = ratchet_violations(xfail_ids, baseline)
    assert not stale, (
        f"baseline entries {sorted(stale)} name case(s) that no longer carry "
        f"xfail in corpus.yaml -- remove them from {DEFAULT_BASELINE_PATH}; the "
        "baseline is shrink-only and must never go stale."
    )


def test_i8_is_not_xfailed() -> None:
    """AC3: I8 fails for real (no xfail) or is removed. It is not removed here."""
    corpus = load_corpus()
    i8 = corpus.by_id("I8")
    assert i8.xfail is None, (
        "I8 still carries an xfail block. Its own prior xfail reason said the "
        "earlier forcing check (OMN-18339) was never real, which is not a "
        "reason to keep suppressing it -- remove the xfail so the nightly can "
        "go red on it for real (OMN-19446 AC3)."
    )


def test_deterministic_must_fail_case_exists_with_no_xfail() -> None:
    """AC1: one case must end in a typed failed terminal, asserted, no xfail."""
    corpus = load_corpus()
    must_fail = [
        c
        for c in corpus.integration_cases()
        if c.expected.terminal == "failed" and c.xfail is None
    ]
    assert must_fail, (
        "no integration case both asserts terminal=failed AND carries no xfail "
        "-- AC1 requires at least one deterministic must-fail case the nightly "
        "cannot suppress."
    )
    # The mechanism must be structural, not model-behavior-dependent: at least
    # one such case must pin an unresolvable backend_id so
    # resolve_delegation_backend raises RuntimeError deterministically
    # (port_local_delegation_dispatch.py OMN-15156: "fail loudly, never a
    # silent fallback") before any live model call happens. I8 is also
    # terminal=failed/xfail=None (AC3), but I8's failure mode still depends on
    # the live escalation ladder (OMN-13543/OMN-13140) -- it is not this
    # case, and must not be mistaken for satisfying AC1's determinism bar.
    deterministic = [c for c in must_fail if c.backend_id]
    assert deterministic, (
        "no must-fail case pins backend_id -- every terminal=failed/no-xfail "
        f"case ({[c.id for c in must_fail]}) depends on live model/ladder "
        "behavior rather than being deterministic (AC1)."
    )


# --- Positive controls: prove the ratchet function can actually fail. ---


def test_positive_control_new_xfail_not_in_baseline_is_a_violation() -> None:
    violations, stale = ratchet_violations(
        live_xfail_ids=frozenset({"I2", "I4", "I99"}),
        baseline_ids=frozenset({"I2", "I4"}),
    )
    assert violations == frozenset({"I99"})
    assert not stale


def test_positive_control_stale_baseline_entry_is_flagged() -> None:
    violations, stale = ratchet_violations(
        live_xfail_ids=frozenset({"I2"}),
        baseline_ids=frozenset({"I2", "I4"}),
    )
    assert not violations
    assert stale == frozenset({"I4"})


def test_positive_control_matching_sets_are_clean() -> None:
    violations, stale = ratchet_violations(
        live_xfail_ids=frozenset({"I2", "I4"}),
        baseline_ids=frozenset({"I2", "I4"}),
    )
    assert not violations
    assert not stale


def test_baseline_entry_missing_ticket_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad_baseline.yaml"
    bad.write_text("known_xfail:\n  - case_id: I2\n")  # no ticket=
    with pytest.raises((KeyError, ValueError)):
        load_baseline(bad)
