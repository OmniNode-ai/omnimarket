# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed TLC evidence for the delegation reaper model (OMN-19441).

The model (one terminal per command id, keyed on the delivering message id and
not on the correlation; the reaper writes no_terminal only into an empty slot,
at or past the deadline measured from the first claim; a terminal that loses the
slot is kept as attempt evidence; every claimed command ends with a terminal of
its own) passes, every mutation fails its named property, and every
reachability witness is violated (the state it negates exists), against the
content digest of the spec and cfg files at the time TLC ran.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "delegation_reaper"
RESULTS = MODEL_DIR / "results"

EXPECTED_VIOLATIONS = {
    "mut_live_upsert": "Invariant AtMostOneTerminal is violated.",
    "mut_ungated_publish": "Invariant WireMatchesRecord is violated.",
    "mut_ungated_handlers": "Invariant AtMostOneTerminal is violated.",
    "mut_no_evidence": "Invariant LateRealKeptAsEvidence is violated.",
    "mut_nonatomic_reap": "Action property SlotWriteOnce is violated.",
    "mut_early_reap": "Action property NoEarlyReap is violated.",
    "mut_refresh_claim": "Action property ClaimTimeFixed is violated.",
    "mut_no_reaper": "Temporal properties were violated.",
    "mut_no_heal": "Temporal properties were violated.",
    "mut_context_last": "Temporal properties were violated.",
    "mut_corr_key": "Temporal properties were violated.",
}

EXPECTED_WITNESSES = {
    "wit_reaped": "Invariant WitReaped is violated.",
    "wit_late_real_evidence": "Invariant WitLateRealEvidence is violated.",
    "wit_timeout_held_past_deadline": "Invariant WitTimeoutHeldPastDeadline is violated.",
    "wit_replay_served": "Invariant WitReplayServed is violated.",
    "wit_shared_correlation_both_answered": (
        "Invariant WitSharedCorrelationBothAnswered is violated."
    ),
    "wit_healed_orphan": "Invariant WitHealedOrphan is violated.",
    "wit_two_real_terminals": "Invariant WitTwoRealTerminals is violated.",
}


def _digest() -> str:
    h = hashlib.sha256()
    h.update((MODEL_DIR / "DelegationReaper.tla").read_bytes())
    for cfg in sorted(MODEL_DIR.glob("*.cfg")):
        h.update(cfg.read_bytes())
    return h.hexdigest()


@pytest.mark.unit
def test_results_bind_to_current_model_digest() -> None:
    assert (RESULTS / "model.sha256").read_text().strip() == _digest()


@pytest.mark.unit
def test_model_holds_every_property() -> None:
    out = (RESULTS / "Model.out").read_text()
    assert "Model checking completed. No error has been found." in out


@pytest.mark.unit
@pytest.mark.parametrize(("name", "message"), sorted(EXPECTED_VIOLATIONS.items()))
def test_each_mutation_fails_its_property(name: str, message: str) -> None:
    out = (RESULTS / f"{name}.out").read_text()
    assert f"Error: {message}" in out
    assert "No error has been found" not in out


@pytest.mark.unit
@pytest.mark.parametrize(("name", "message"), sorted(EXPECTED_WITNESSES.items()))
def test_each_reachability_witness_is_reached(name: str, message: str) -> None:
    out = (RESULTS / f"{name}.out").read_text()
    assert f"Error: {message}" in out


@pytest.mark.unit
def test_every_mutation_and_witness_cfg_has_a_result() -> None:
    assert {p.stem for p in MODEL_DIR.glob("mut_*.cfg")} == set(EXPECTED_VIOLATIONS)
    assert {p.stem for p in MODEL_DIR.glob("wit_*.cfg")} == set(EXPECTED_WITNESSES)
    for stem in (*EXPECTED_VIOLATIONS, *EXPECTED_WITNESSES):
        assert (RESULTS / f"{stem}.out").is_file()


@pytest.mark.unit
def test_the_model_is_keyed_per_command_not_per_correlation() -> None:
    model_cfg = (MODEL_DIR / "Model.cfg").read_text()
    assert "KeyByCorrelation = FALSE" in model_cfg
    assert "KeyByCorrelation = TRUE" in (MODEL_DIR / "mut_corr_key.cfg").read_text()
