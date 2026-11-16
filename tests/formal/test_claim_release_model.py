# SPDX-License-Identifier: MIT
"""OMN-23737: the PR-claim release model, its recorded TLC results and its verdict agree.

CI has no java, so the evidence is the recorded run: ``results/SUMMARY.txt`` holds the
sha256 of the model, of each config and of each config's output (the command that made
them is in ``README.txt``). The committed results must be for the committed model and
configs. ``Model.cfg`` must pass every invariant and the liveness property; each
mutation config flips exactly one knob of ``Model.cfg`` to FALSE and must violate the one
property it checks; each witness must violate its invariant, which shows the state is
reachable. Rerun the README command after changing the model or a config: the digest
tests below fail until the results are regenerated for the changed file.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "docs" / "formal" / "claim_release"
RESULTS = MODEL_DIR / "results"
MODEL = "ClaimRelease.tla"
ROW_RE = re.compile(
    r"^(?P<name>\S+) cfg_sha256=(?P<cfg>\w+) out_sha256=(?P<out>\w+) result=(?P<result>.+)$"
)

INVARIANTS = (
    "TypeOK",
    "NeverReleaseLiveHolder",
    "ReleasedOnlyAfterExitSeen",
    "ClosesOwnOnly",
    "DeletesWhatItInspected",
)
LIVENESS = "NoStrandedClaim"
# mutation config -> (the knob it flips, the one property it checks)
MUTATIONS = {
    "mut_no_exit_check": ("RequireExit", "NeverReleaseLiveHolder"),
    "mut_no_exit_evidence": ("RequireExit", "ReleasedOnlyAfterExitSeen"),
    "mut_lane_name_match": ("ExactIdentity", "ClosesOwnOnly"),
    "mut_delete_without_compare": ("CompareBeforeDelete", "DeletesWhatItInspected"),
    "mut_no_live_heartbeat": ("LiveHeartbeats", "NeverReleaseLiveHolder"),
    "mut_closeout_never_runs": ("CloseoutRuns", LIVENESS),
}
WITNESSES = {
    "wit_closeout_releases": "NeverCloseout",
    "wit_expiry_reaps_ended": "NeverExpiry",
    "wit_replacement_holds_after_closeout": "NeverReplacementHoldsAfterCloseout",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, dict[str, str]]]:
    model_sha = ""
    rows: dict[str, dict[str, str]] = {}
    for line in (RESULTS / "SUMMARY.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("model:"):
            model_sha = line.split("sha256=")[1].strip()
        elif (m := ROW_RE.match(line)) is not None:
            rows[m["name"]] = {
                "cfg_sha256": m["cfg"],
                "out_sha256": m["out"],
                "result": m["result"],
            }
    return model_sha, rows


def _cfg_line(name: str, keyword: str) -> list[str]:
    text = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
    found = re.search(rf"^{keyword} (.*)$", text, re.MULTILINE)
    return found.group(1).split() if found else []


def _knobs(name: str) -> dict[str, str]:
    text = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
    return dict(re.findall(r"^(?:CONSTANTS)?\s*(\w+) = (\w+)$", text, re.MULTILINE))


def _out_exit(name: str) -> int:
    return int(
        (RESULTS / f"{name}.out").read_text(encoding="utf-8").splitlines()[-1][5:]
    )


def test_claim_release_results_bind_to_the_current_model() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL), "model changed: rerun TLC (README.txt)"
    for name, cells in rows.items():
        assert cells["cfg_sha256"] == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert cells["out_sha256"] == _sha(RESULTS / f"{name}.out"), name


def test_claim_release_every_config_has_a_recorded_result() -> None:
    _, rows = _summary()
    assert {path.stem for path in MODEL_DIR.glob("*.cfg")} == set(rows)


def test_claim_release_model_cfg_passes_every_property_and_terminates() -> None:
    _, rows = _summary()
    assert rows["Model"]["result"] == "PASS"
    assert tuple(_cfg_line("Model", "INVARIANTS")) == INVARIANTS
    assert _cfg_line("Model", "PROPERTIES") == [LIVENESS]
    out = (RESULTS / "Model.out").read_text(encoding="utf-8")
    assert "No error has been found" in out
    assert _out_exit("Model") == 0


@pytest.mark.parametrize(("name", "knob_and_property"), sorted(MUTATIONS.items()))
def test_claim_release_each_mutation_fails_its_own_property(
    name: str, knob_and_property: tuple[str, str]
) -> None:
    knob, prop = knob_and_property
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {prop}"
    model, mutant = _knobs("Model"), _knobs(name)
    changed = {k for k in model if model[k] != mutant.get(k)}
    assert changed == {knob}, "a mutation flips exactly one knob"
    assert mutant[knob] == "FALSE"
    checked = _cfg_line(name, "INVARIANTS") + _cfg_line(name, "PROPERTIES")
    assert prop in checked
    assert set(checked) <= {"TypeOK", prop}, "a mutation checks the one property"
    assert _out_exit(name) == (13 if prop == LIVENESS else 12)


@pytest.mark.parametrize(("name", "invariant"), sorted(WITNESSES.items()))
def test_claim_release_each_witness_reaches_its_state(
    name: str, invariant: str
) -> None:
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {invariant}"
    assert invariant in _cfg_line(name, "INVARIANTS")
    assert _knobs(name) == _knobs("Model"), "a witness runs the base design"


def test_claim_release_planted_live_release_is_a_named_violation() -> None:
    out = (RESULTS / "mut_no_exit_check.out").read_text(encoding="utf-8")
    assert "Invariant NeverReleaseLiveHolder is violated." in out
    assert '"live"' in out, "the trace releases a holder whose lane is live"


def test_claim_release_verdict_and_lab_reading_are_recorded() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text(encoding="utf-8")
    assert verdict.startswith("Design-review verdict")
    assert re.search(
        r"^Verdict: (PASS|PASS with conditions|FAIL)", verdict, re.MULTILINE
    )
    for name in (*MUTATIONS, *WITNESSES):
        assert name in verdict
    reading = (RESULTS / "lab_reading_h053.txt").read_text(encoding="utf-8")
    assert "host: h053" in reading
    assert "claim directory:" in reading
    assert "heartbeat_written = False" in reading
