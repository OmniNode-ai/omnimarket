# SPDX-License-Identifier: MIT
"""OMN-22429: host-state models, mutant evidence and reviewed bytes agree.

These tests read the committed TLC evidence without requiring Java on the hosted
runner. SHA256SUMS binds each model, configuration, output and verdict to the
evidence set validated on the lab host.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

FORMAL = Path(__file__).resolve().parents[2] / "docs" / "formal"
MODELS = {
    "host_desired_state": "HostDesiredState.tla",
    "host_converge": "HostConverge.tla",
}
MUTANTS = {
    "host_desired_state": {
        "GenerationMonotone": "Fenced",
        "OnePublisherAccepted": "ReaderFilters",
        "PendingTagsBeforePublish": "TagsFirst",
        "PendingRepublishedExactly": "ResumeExact",
        "ReleasedLayerNeverReturns": "Tombstones",
        "DependentLeavesWithDependency": "Cascade",
        "StaleRequestNeverApplies": "CheckRevision",
        "NoLaunchOnExpiredLayer": "CheckValidUntil",
    },
    "host_converge": {
        "NoObservedBeforeReadback": "PublishAfterReadback",
        "NoLaneOnUnverifiedTree": "RecheckUnderFence",
        "PinnedTreeNeverRemoved": "CollectRespectsPins",
        "OrphanPinReleased": "PinChecksStart",
        "OwnedSurfaceOneWriter": "UnguardedFixOn",
        "ForeignWriteReported": "ReadbackCompares",
        "TakeoverBeforeMutation": "TakeoverFirst",
        "StateWritesSerialised": "StateLock",
        "EverySurfaceWithinTwoPasses": "OrderByWait",
    },
}
pytestmark = pytest.mark.unit


def _invariants(cfg: str) -> set[str]:
    return set(cfg.split("INVARIANTS", 1)[1].split("CHECK_DEADLOCK", 1)[0].split())


@pytest.mark.parametrize("model", MODELS)
def test_design_evidence_matches_committed_bytes(model: str) -> None:
    directory = FORMAL / model
    expected = {
        MODELS[model],
        "Model.cfg",
        "results/Model.out",
        "results/EXACTNESS.txt",
        "results/design_review_verdict.txt",
        *(f"mut_{inv}.cfg" for inv in MUTANTS[model]),
        *(f"results/mut_{inv}.out" for inv in MUTANTS[model]),
    }
    rows = {}
    for row in (directory / "results" / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", row)
        assert match, row
        digest, name = match.groups()
        assert name in expected, name
        assert name not in rows, name
        rows[name] = digest
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    assert set(rows) == expected


@pytest.mark.parametrize("model", MODELS)
def test_design_passes_all_invariants(model: str) -> None:
    directory = FORMAL / model
    cfg = (directory / "Model.cfg").read_text()
    assert _invariants(cfg) == {"TypeOK", *MUTANTS[model]}
    assert "SPECIFICATION Spec" in cfg
    output = (directory / "results" / "Model.out").read_text()
    assert "Model checking completed. No error has been found." in output
    assert "Error:" not in output
    assert output.rstrip().endswith("exit=0")
    assert re.search(
        r"[1-9][0-9,]* distinct states found, 0 states left on queue", output
    )


@pytest.mark.parametrize(
    ("model", "invariant", "knob"),
    [
        (model, inv, knob)
        for model, mutations in MUTANTS.items()
        for inv, knob in mutations.items()
    ],
)
def test_mutant_changes_one_knob_and_violates_only_its_invariant(
    model: str, invariant: str, knob: str
) -> None:
    directory = FORMAL / model
    design = (directory / "Model.cfg").read_text()
    mutant = (directory / f"mut_{invariant}.cfg").read_text()
    original = "FALSE" if knob == "UnguardedFixOn" else "TRUE"
    mutated = "TRUE" if original == "FALSE" else "FALSE"
    assert design.count(f"{knob} = {original}") == 1
    assert mutant == design.replace(f"{knob} = {original}", f"{knob} = {mutated}")
    assert _invariants(mutant) == {"TypeOK", *MUTANTS[model]}
    output = (directory / "results" / f"mut_{invariant}.out").read_text()
    assert re.findall(r"Error: Invariant (\w+) is violated\.", output) == [invariant]
    assert "No error has been found" not in output
    assert output.rstrip().endswith("exit=12")
    assert "Error: The behavior up to this point is:" in output


@pytest.mark.parametrize("model", MODELS)
def test_mutant_inventory_and_full_state_space_exactness(model: str) -> None:
    directory = FORMAL / model
    assert {p.stem for p in directory.glob("mut_*.cfg")} == {
        f"mut_{inv}" for inv in MUTANTS[model]
    }
    assert {p.stem for p in (directory / "results").glob("mut_*.out")} == {
        f"mut_{inv}" for inv in MUTANTS[model]
    }
    rows = [
        line
        for line in (directory / "results" / "EXACTNESS.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert len(rows) == len(MUTANTS[model])
    seen = set()
    for row in rows:
        match = re.fullmatch(r"mut_(\w+): (\w+)\(([1-9][0-9]*)\)\s*", row)
        assert match, row
        target, violated, _count = match.groups()
        assert target == violated, row
        assert target in MUTANTS[model], row
        seen.add(target)
    assert seen == set(MUTANTS[model])


@pytest.mark.parametrize("model", MODELS)
def test_verdict_approves_the_current_model(model: str) -> None:
    directory = FORMAL / model
    verdict = (directory / "results" / "design_review_verdict.txt").read_text()
    assert verdict.splitlines()[-1] == "VERDICT: PASS"
    assert verdict.splitlines().count("VERDICT: PASS") == 1
    reviewed = re.search(r"sha256 of the model: ([0-9a-f]{64})\.", verdict)
    assert reviewed
    assert (
        reviewed.group(1)
        == hashlib.sha256((directory / MODELS[model]).read_bytes()).hexdigest()
    )
