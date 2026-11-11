# SPDX-License-Identifier: MIT
"""The lane start model, its recorded TLC results and its design review agree.

CI has no java, so the evidence is the recorded run: ``lane_start_tlc.py`` writes
``SUMMARY.txt`` with the digest of the model, of each config and of each config's output.
The committed results must be for the committed model and configs; ``Model.cfg`` must pass
every invariant and the liveness property; each mutation must fail exactly the property
named for it while every invariant of its base config stays enabled; each witness must
violate its invariant, which shows the state is reachable. Rerun
``tests/formal/lane_start_tlc.py`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "docs" / "formal" / "lane_start"
RESULTS = MODEL_DIR / "results"
MODEL = "LaneStart.tla"
ROW_RE = re.compile(
    r"^(?P<name>\S+) cfg_sha256=(?P<cfg>\w+) out_sha256=(?P<out>\w+) result=(?P<result>.+)$"
)

MUTATIONS = {
    "mut_retry_any": ({"RetryGuard"}, "Model", "StartedAtMostOnce"),
    "mut_retry_after_started": ({"RetryGuard"}, "Model", "StartedAtMostOnce"),
    "mut_retry_inflight": ({"RetryInflight"}, "Model", "StartedAtMostOnce"),
    "mut_engine_before_claim": ({"ClaimFirst"}, "Model", "ClaimBeforeEngine"),
    "mut_no_single_closer": ({"SingleCloser"}, "Model", "AtMostOneTerminal"),
    "mut_terminal_without_claim": (
        {"TerminalNeedsClaim"},
        "Model",
        "NoTerminalWithoutClaim",
    ),
    "mut_observe_claims": ({"ObserveGuard"}, "obs_model", "ObserveWritesNothing"),
    "mut_no_reaper": ({"Reaper"}, "Model", "StartedReachesTerminal"),
    "mut_stopper_closes_prestart": (
        {"StopperWaitsLeader"},
        "Model",
        "LeaderTerminalAfterExit",
    ),
    # A hazard under an assumption the model states (LateStart = FALSE), not a design knob.
    "haz_late_start": ({"LateStart"}, "Model", "LeaderTerminalAfterExit"),
    # What the code does today: each is expected VIOLATED until the piece it names is built.
    "asbuilt_stopper": ({"StopperCode"}, "Model", "LeaderTerminalAfterExit"),
    "asbuilt_stopper_liveness": ({"StopperCode"}, "Model", "StartedReachesTerminal"),
    "asbuilt_outcomes": (
        {"CodeOutcomes", "RetryGuard"},
        "Model",
        "StartedAtMostOnce",
    ),
}
# A liveness mutant names the one property it checks, so its INVARIANTS and PROPERTIES lines
# differ from the base by design.
CHECK_LINES = ("INVARIANTS", "PROPERTIES")
LIVENESS = ("mut_no_reaper", "asbuilt_stopper_liveness")
WITNESSES = {
    "wit_NeverStarted": ("Model", "NeverStarted"),
    "wit_NeverClosedByLane": ("Model", "NeverClosedByLane"),
    "wit_NeverClosedByLeader": ("Model", "NeverClosedByLeader"),
    "wit_NeverClosedByStopper": ("Model", "NeverClosedByStopper"),
    "wit_NeverStartFailed": ("Model", "NeverStartFailed"),
    "wit_NeverRetriedAfterFailedStart": ("Model", "NeverRetriedAfterFailedStart"),
    "wit_NeverUnresolvedRan": ("Model", "NeverUnresolvedRan"),
    "wit_NeverLeaderStatus": ("Model", "NeverLeaderStatus"),
    "wit_NeverRetryAfterRefused": ("Model", "NeverRetryAfterRefused"),
    "wit_NeverThirdStarted": ("Model", "NeverThirdStarted"),
    "wit_NeverStopperAfterDeadLeaderRan": (
        "Model",
        "NeverStopperAfterDeadLeaderRan",
    ),
    "wit_NeverObserved": ("obs_model", "NeverObserved"),
}
INVARIANTS = (
    "TypeOK",
    "StartedAtMostOnce",
    "ClaimBeforeEngine",
    "NoTerminalWithoutClaim",
    "AtMostOneTerminal",
    "StartedTerminalPerKey",
    "LeaderTerminalAfterExit",
    "ObserveWritesNothing",
)
PROPERTIES = (
    "PROPERTIES StartedReachesTerminal ClaimReachesTerminal NoStartAfterTerminal"
)


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


def _cfg_invariants(name: str) -> list[str]:
    text = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
    found = re.search(r"^INVARIANTS (.*)$", text, re.MULTILINE)
    return found.group(1).split() if found else []


def _knob_diff(name: str, base: str) -> set[str]:
    """The constants whose value differs between a config and its base (check lines aside)."""
    lines = (MODEL_DIR / f"{base}.cfg").read_text(encoding="utf-8").splitlines()
    other = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(other), f"{name} and {base} differ in length"
    changed: set[str] = set()
    for a, b in zip(lines, other, strict=True):
        if a != b and not a.startswith(CHECK_LINES):
            changed.add(a.replace("CONSTANTS", "").split("=")[0].strip())
    return changed


def test_lane_start_results_bind_to_the_current_model() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL), (
        "model changed: rerun lane_start_tlc.py"
    )
    for name, cells in rows.items():
        assert cells["cfg_sha256"] == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert cells["out_sha256"] == _sha(RESULTS / f"{name}.out"), name


def test_lane_start_every_config_has_a_recorded_result() -> None:
    _, rows = _summary()
    configs = {path.stem for path in MODEL_DIR.glob("*.cfg")}
    assert configs == set(rows)


def test_lane_start_model_cfg_passes_every_invariant_and_terminates() -> None:
    _, rows = _summary()
    for name in ("Model", "obs_model"):
        assert rows[name]["result"] == "PASS", name
        assert tuple(_cfg_invariants(name)) == INVARIANTS, name
        assert PROPERTIES in (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8"), (
            name
        )
    out = (RESULTS / "Model.out").read_text(encoding="utf-8")
    assert "No error has been found" in out


@pytest.mark.parametrize(("name", "knobs_and_property"), sorted(MUTATIONS.items()))
def test_lane_start_each_mutation_fails_its_own_property(
    name: str, knobs_and_property: tuple[set[str], str, str]
) -> None:
    knobs, base, prop = knobs_and_property
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {prop}"
    assert _knob_diff(name, base) == knobs, "a mutation flips exactly its named knobs"
    if name not in LIVENESS:
        assert _cfg_invariants(name) == _cfg_invariants(base), (
            "a safety mutation keeps every invariant of its base"
        )


def test_lane_start_unresolved_key_stays_dead() -> None:
    _, rows = _summary()
    assert rows["chk_unresolved_stays_dead"]["result"] == "PASS"


@pytest.mark.parametrize(("name", "invariant"), sorted(WITNESSES.items()))
def test_lane_start_each_witness_reaches_its_state(
    name: str, invariant: tuple[str, str]
) -> None:
    base, prop = invariant
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {prop}"
    wit_text = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
    assert not re.search(r"^PROPERTIES ", wit_text, re.MULTILINE), (
        "a witness cfg carries no PROPERTIES line"
    )
    base_lines = [
        line
        for line in (MODEL_DIR / f"{base}.cfg").read_text(encoding="utf-8").splitlines()
        if not line.startswith("PROPERTIES")
    ]
    wit_lines = [
        line for line in wit_text.splitlines() if not line.startswith("PROPERTIES")
    ]
    changed = [(a, b) for a, b in zip(base_lines, wit_lines, strict=True) if a != b]
    assert len(changed) == 1, "a witness changes only the INVARIANTS line"
    assert changed[0][0].split()[0] == "INVARIANTS"


def test_lane_start_design_review_verdict_is_recorded() -> None:
    verdict = (RESULTS / "design_review_verdict.txt").read_text(encoding="utf-8")
    assert verdict.startswith("# Design-review verdict")
    for name in MUTATIONS:
        assert name in verdict
    assert "Verdict:" in verdict
