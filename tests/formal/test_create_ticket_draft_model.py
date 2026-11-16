# SPDX-License-Identifier: MIT
"""OMN-23760: the create-ticket draft model, its TLC results and its verdict agree.

The design configs must pass; each mutant, hazard and witness must end in a TLC
violation naming the property it targets; SUMMARY.txt must bind the model and every
config by sha256, so a changed model or config without a rerun fails here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "create_ticket_draft"
RESULTS = MODEL_DIR / "results"
SUMMARY_PATH = "formal/create_ticket_draft/results/SUMMARY.txt"

PASS_CONFIGS = (
    "Model",
    "Model_live",
    "Model_2draft_crash",
    "Model_2draft_fail",
    "Model_2draft_dupfail",
    "alt_stamp_serial",
)
INVARIANTS = (
    "AtMostOneIssuePerCid",
    "OneTerminalPerRun",
    "DoneHasTerminal",
    "TerminalsTyped",
    "WrittenFollowsWrite",
    "WrittenHashesReturned",
    "WrittenCertifiesBlock",
    "OneWrittenPerCid",
    "CompletedHasBlock",
    "CompletedHasWritten",
    "CompletedNoProject",
)
MUTANTS = {
    "mut_stamp_search_concurrent": "AtMostOneIssuePerCid",
    "mut_find_lag": "AtMostOneIssuePerCid",
    "mut_stamp_after_create": "AtMostOneIssuePerCid",
    "mut_alt_write_drops_stamp": "AtMostOneIssuePerCid",
    "mut_resume_trusts_issue": "CompletedHasBlock",
    "mut_resume_skips_publish": "CompletedHasWritten",
    "mut_resume_skips_clear": "CompletedNoProject",
    "mut_readback_unchecked": "WrittenCertifiesBlock",
    "mut_publish_before_write": "WrittenFollowsWrite",
    "mut_hash_of_sent": "WrittenHashesReturned",
    "mut_swallow_error": "DoneHasTerminal",
    "mut_event_id_per_run": "OneWrittenPerCid",
    "mut_fp_unstable": "OneWrittenPerCid",
}
TEMPORAL_MUTANTS = {
    "mut_conflict_loops": "RunEnds",
    "mut_no_redelivery": "CommandAnswered",
}
HAZARDS = {
    "haz_fail_after_complete": "NoFailAfterComplete",
    "haz_block_without_event": "BlockHasEventWhenQuiet",
}
WITNESSES = (
    "NeverCompleted",
    "NeverFailed",
    "NeverHandEdited",
    "NeverIdUnreadable",
    "NeverRerunCompletes",
    "NeverCreateFailAfterThenRerun",
    "NeverTwoActive",
    "NeverReplayPublish",
    "NeverConflict",
    "NeverUnacked",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary_line(cfg: str) -> str:
    for line in _read(RESULTS / "SUMMARY.txt").splitlines():
        if line.startswith(f"{cfg} "):
            return line
    raise AssertionError(f"no SUMMARY line for {cfg}")


@pytest.mark.parametrize("cfg", PASS_CONFIGS)
def test_design_configs_pass(cfg: str) -> None:
    out = _read(RESULTS / f"{cfg}.out")
    assert "Model checking completed. No error has been found." in out
    assert "exit=0" in out.splitlines()
    assert "result=PASS" in _summary_line(cfg)


def test_model_config_checks_eleven_invariants_and_live_config_the_liveness() -> None:
    lines = _read(MODEL_DIR / "Model.cfg").splitlines()
    assert "INVARIANTS " + " ".join(INVARIANTS) in lines
    assert (
        "PROPERTIES RunEnds CommandAnswered"
        in _read(MODEL_DIR / "Model_live.cfg").splitlines()
    )


@pytest.mark.parametrize(("cfg", "prop"), sorted({**MUTANTS, **HAZARDS}.items()))
def test_invariant_mutant_or_hazard_violates_its_property(cfg: str, prop: str) -> None:
    assert f"INVARIANTS {prop}" in _read(MODEL_DIR / f"{cfg}.cfg").splitlines()
    assert f"Error: Invariant {prop} is violated" in _read(RESULTS / f"{cfg}.out")
    assert f"result=VIOLATED {prop}" in _summary_line(cfg)


@pytest.mark.parametrize(("cfg", "prop"), sorted(TEMPORAL_MUTANTS.items()))
def test_temporal_mutant_violates_its_property(cfg: str, prop: str) -> None:
    assert f"PROPERTIES {prop}" in _read(MODEL_DIR / f"{cfg}.cfg").splitlines()
    assert "Error: Temporal properties were violated" in _read(RESULTS / f"{cfg}.out")
    assert f"result=VIOLATED {prop}" in _summary_line(cfg)


@pytest.mark.parametrize("name", WITNESSES)
def test_witness_violation_shows_the_state_is_reachable(name: str) -> None:
    out = _read(RESULTS / f"wit_{name}.out")
    assert f"Error: Invariant {name} is violated" in out
    assert f"result=VIOLATED {name}" in _summary_line(f"wit_{name}")


def test_summary_binds_the_model_and_every_config() -> None:
    lines = _read(RESULTS / "SUMMARY.txt").splitlines()
    assert (
        lines[0]
        == f"model: CreateTicketDraft.tla sha256={_sha(MODEL_DIR / 'CreateTicketDraft.tla')}"
    )
    cfgs = sorted(MODEL_DIR.glob("*.cfg"))
    assert len(lines) - 1 == len(cfgs)
    for cfg in cfgs:
        assert f"cfg_sha256={_sha(cfg)}" in _summary_line(cfg.stem)


@pytest.mark.parametrize("name", ["README.txt", "VERDICT.txt"])
def test_docs_cite_the_receipt(name: str) -> None:
    assert SUMMARY_PATH in _read(MODEL_DIR / name)


def test_verdict_names_digest_and_every_mutant_result() -> None:
    text = _read(MODEL_DIR / "VERDICT.txt")
    assert f"sha256={_sha(MODEL_DIR / 'CreateTicketDraft.tla')}" in text
    for cfg, prop in {**MUTANTS, **TEMPORAL_MUTANTS}.items():
        assert any(cfg in ln and prop in ln for ln in text.splitlines()), cfg
