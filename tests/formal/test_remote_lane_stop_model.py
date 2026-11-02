# SPDX-License-Identifier: MIT
"""The remote-lane stop model, its recorded TLC results and its design review agree.

CI has no java, so the evidence is the recorded run: ``remote_lane_stop_tlc.sh`` writes
``SUMMARY.txt`` with the digest of the model, of each config and of each config's output.
The committed results must be for the committed model and configs; ``Model.cfg`` must pass
every invariant and the liveness property; each mutation must fail exactly the property
named for it while every invariant of ``Model.cfg`` stays enabled; each witness must
violate its invariant, which shows the state is reachable. Rerun
``tests/formal/remote_lane_stop_tlc.sh`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "docs" / "formal" / "remote_lane_stop"
RESULTS = MODEL_DIR / "results"
MODEL = "RemoteLaneStop.tla"
ROW_RE = re.compile(
    r"^(?P<name>\S+) cfg_sha256=(?P<cfg>\w+) out_sha256=(?P<out>\w+) result=(?P<result>.+)$"
)

MUTATIONS = {
    "mut_no_single_closer": ("SingleCloser", "AtMostOneTerminal"),
    "mut_no_wait_for_exit": ("WaitForExit", "TerminalAfterExit"),
    "mut_early_kill": ("WaitBeforeKill", "KillAfterWait"),
    "mut_signal_first": ("CheckFirst", "NoSignalWhenAlreadyEnded"),
    "mut_no_give_up": ("GiveUp", "EveryStopAnswered"),
}
WITNESSES = {
    "wit_node_stops": "NeverStopped",
    "wit_loser_noop": "NeverLoser",
    "wit_escalated_then_stopped": "NeverEscalatedAndStopped",
    "wit_survivor_refused": "NeverRefused",
    "wit_ended_read_no_signal": "NeverReadEnded",
    "wit_runner_beat_signalled_stopper": "NeverRunnerWonOverStopper",
}
INVARIANTS = (
    "TypeOK",
    "AtMostOneTerminal",
    "TerminalAfterExit",
    "StoppedIsOwnWrite",
    "LoserWritesNothing",
    "RefusedWritesNothing",
    "EndedHasTerminal",
    "NoSignalWhenAlreadyEnded",
    "KillAfterWait",
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


def test_remote_lane_stop_results_bind_to_the_current_model() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL), (
        "model changed: rerun remote_lane_stop_tlc.sh"
    )
    for name, cells in rows.items():
        assert cells["cfg_sha256"] == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert cells["out_sha256"] == _sha(RESULTS / f"{name}.out"), name


def test_remote_lane_stop_every_config_has_a_recorded_result() -> None:
    _, rows = _summary()
    configs = {path.stem for path in MODEL_DIR.glob("*.cfg")}
    assert configs == set(rows)


def test_remote_lane_stop_model_cfg_passes_every_invariant_and_terminates() -> None:
    _, rows = _summary()
    assert rows["Model"]["result"] == "PASS"
    assert tuple(_cfg_invariants("Model")) == INVARIANTS
    assert "PROPERTIES EveryStopAnswered" in (MODEL_DIR / "Model.cfg").read_text(
        encoding="utf-8"
    )
    out = (RESULTS / "Model.out").read_text(encoding="utf-8")
    assert "No error has been found" in out


@pytest.mark.parametrize(("name", "knob_and_property"), sorted(MUTATIONS.items()))
def test_remote_lane_stop_each_mutation_fails_its_own_property(
    name: str, knob_and_property: tuple[str, str]
) -> None:
    knob, prop = knob_and_property
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {prop}"
    model = (MODEL_DIR / "Model.cfg").read_text(encoding="utf-8").splitlines()
    mutant = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(model, mutant, strict=True) if a != b]
    assert len(changed) == 1, "a mutation flips exactly one knob"
    assert changed[0][0].split("=")[0].split()[-1] == knob
    assert changed[0][1].strip().endswith("FALSE")
    assert _cfg_invariants(name) == _cfg_invariants("Model"), (
        "a mutation keeps every invariant"
    )


@pytest.mark.parametrize(("name", "invariant"), sorted(WITNESSES.items()))
def test_remote_lane_stop_each_witness_reaches_its_state(
    name: str, invariant: str
) -> None:
    _, rows = _summary()
    assert rows[name]["result"] == f"VIOLATED {invariant}"


def test_remote_lane_stop_design_review_verdict_is_recorded() -> None:
    verdict = (RESULTS / "design_review_verdict.txt").read_text(encoding="utf-8")
    assert verdict.startswith("# Design-review verdict")
    for name in MUTATIONS:
        assert name in verdict
    assert "Verdict:" in verdict
