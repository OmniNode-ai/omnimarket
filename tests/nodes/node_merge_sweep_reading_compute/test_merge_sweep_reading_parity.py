# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the reader node reads a sweep as the old reader did, from the watcher state (OMN-20676).

``fixtures/recorded_sweep.json`` was recorded by running the merge-sweep skill's own
``sweep_read.live_reading`` over a trimmed snapshot of the PR watcher's state file, with its GitHub
layer served from that snapshot (no network): one scenario is the snapshot whole, the others are
generated variants (drafts, run overrides, changed files, ``ready_at``, merges, ledger rows,
controller ticks, chains). The expected reading and plan in each scenario are the old tools' own.
The chain under test is the production one: the effect node loads the files, the compute node reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_effect.handlers import HandlerMergeSweepLoadFacts
from omnimarket.nodes.node_merge_sweep_effect.models import ModelMergeSweepLoadRequest
from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    HandlerMergeSweepPlan,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
)
from omnimarket.nodes.node_merge_sweep_reading_compute.handlers import (
    HandlerMergeSweepClaimCheck,
    HandlerMergeSweepRead,
    handler_merge_sweep_read,
)
from omnimarket.nodes.node_merge_sweep_reading_compute.models import (
    ModelMergeSweepReadRequest,
    ModelMergeSweepReadResult,
)

from .sweep_scenarios import load_recorded, write_files

RECORDED = load_recorded()
BASE = RECORDED["base"]
SCENARIOS = RECORDED["scenarios"]
READING_KEYS = ("product", "controller", "reds", "chain_heads", "escalations")


def _facts(spec: dict[str, Any], root: Path) -> ModelMergeSweepReadRequest:
    """Load the scenario's files through the effect node, then add what the state lacks."""
    paths = write_files(BASE, spec, root)
    loaded = HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(paths["state"]),
            ledger_path=str(paths["ledger"]),
            ticks_path=str(paths["ticks"]) if "ticks" in paths else None,
            floors_path=str(paths["floors"]),
            now=spec["now"],
            load1=spec["load1"],
            cpus=spec["cpus"],
            window_min=spec["window_min"],
            max_reds=spec["max_reds"],
        )
    )
    assert loaded.ok, loaded.why
    assert loaded.facts is not None
    facts = loaded.facts
    # The watcher records neither a PR's changed files, its last ready_for_review time nor a merge's
    # changed files; the scenario supplies them the way a richer source would.
    files, ready = spec.get("files", {}), spec.get("ready_at", {})
    open_prs = [
        p.model_copy(
            update={
                "files": files.get(f"{p.repo}#{p.number}", []),
                "ready_at": ready.get(f"{p.repo}#{p.number}"),
                "facts_unread": [],
            }
        )
        for p in facts.open_prs
    ]
    merge_files = spec.get("merge_files", {})
    merges = [
        m.model_copy(update={"files": merge_files.get(f"{m.repo}#{m.number}")})
        for m in facts.merges
    ]
    return facts.model_copy(update={"open_prs": open_prs, "merges": merges})


def _read(spec: dict[str, Any], tmp_path: Path) -> ModelMergeSweepReadResult:
    return HandlerMergeSweepRead().handle(_facts(spec, tmp_path))


def test_the_recorded_sweep_covers_every_branch() -> None:
    assert len(SCENARIOS) >= 50
    assert SCENARIOS[0]["id"] == "recorded-snapshot"
    assert len(BASE["prs"]) >= 40
    readings = [s["expected"]["reading"] for s in SCENARIOS]
    classes = {
        c["cls"] for r in readings for red in r["reds"] for c in red["classes"] or []
    }
    assert classes >= {
        "real",
        "draft-era",
        "cancelled-latest",
        "stale-base-sql",
        "workflow-scope-arm",
    }
    assert {o["owner"]["state"] for r in readings for o in r["reds"]} >= {
        "none",
        "live",
        "stale",
    }
    assert any(r["chain_heads"] for r in readings)
    assert any(r["escalations"] for r in readings)
    assert any(r["product"]["excluded"]["docs_only"] for r in readings)
    assert any(r["product"]["excluded"]["files_unread"] for r in readings)
    assert any(r["controller"]["stalled"] for r in readings)
    assert any(not r["controller"]["stalled"] for r in readings)
    assert any(red["classes"] is None for r in readings for red in r["reds"])
    kinds = {lane["kind"] for s in SCENARIOS for lane in s["expected"]["plan"]["lanes"]}
    assert kinds == {"diagnose", "escalation", "land-chain-head", "fix"}


@pytest.mark.parametrize("index", range(len(SCENARIOS)))
def test_reading_matches_the_old_reader(index: int, tmp_path: Path) -> None:
    spec = SCENARIOS[index]
    got = _read(spec, tmp_path).model_dump()
    expected = spec["expected"]["reading"]
    assert {k: got[k] for k in READING_KEYS} == {k: expected[k] for k in READING_KEYS}
    assert got["open_prs"] == expected["open_prs"]


@pytest.mark.parametrize("index", range(len(SCENARIOS)))
def test_plan_over_the_reading_matches_the_old_plan(index: int, tmp_path: Path) -> None:
    spec = SCENARIOS[index]
    reading = _read(spec, tmp_path)
    planned = HandlerMergeSweepPlan().handle(
        ModelMergeSweepPlanRequest.model_validate({"reading": reading.model_dump()})
    )
    got = planned.model_dump(exclude_unset=True)
    expected = spec["expected"]["plan"]
    assert {k: got[k] for k in ("lab_only", "lanes", "skipped")} == {
        k: expected[k] for k in ("lab_only", "lanes", "skipped")
    }
    assert got["load_per_core"] == pytest.approx(expected["load_per_core"])


def test_an_unread_fact_is_listed_and_never_a_zero(tmp_path: Path) -> None:
    """What the watcher does not record is named, on the state as loaded."""
    spec = SCENARIOS[0]
    paths = write_files(BASE, spec, tmp_path)
    loaded = HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(paths["state"]),
            ledger_path=str(paths["ledger"]),
            ticks_path=None,
            floors_path=str(paths["floors"]),
            now=spec["now"],
            load1=1.0,
            cpus=8,
        )
    )
    assert loaded.facts is not None
    result = HandlerMergeSweepRead().handle(loaded.facts)
    fields = {u.field for u in result.unread}
    assert "controller" in fields
    assert "merge-files" in fields
    assert any(f.startswith("ready_at:") for f in fields)
    assert any(f.startswith("files:") for f in fields)
    assert result.product["excluded"]["files_unread"] > 0
    assert result.controller["stalled"] is True


def test_a_broken_owner_gate_fails_the_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: a claim owner that never reads live disagrees with the old reader."""
    rules = handler_merge_sweep_read.rules
    real = rules.claim_owner

    def never_live(*args: Any, **kwargs: Any) -> dict[str, Any]:
        owner = real(*args, **kwargs)
        return {"state": "none"} if owner.get("state") == "live" else owner

    monkeypatch.setattr(rules, "claim_owner", never_live)
    misses = 0
    for index, spec in enumerate(SCENARIOS):
        got = _read(spec, tmp_path / str(index)).model_dump()
        misses += got["reds"] != spec["expected"]["reading"]["reds"]
    assert misses > 0


def test_a_broken_class_rule_fails_the_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: reading every failure as real disagrees with the old classes."""
    rules = handler_merge_sweep_read.rules
    real = rules.classify_checks

    def all_real(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{**c, "cls": "real"} for c in real(*args, **kwargs)]

    monkeypatch.setattr(rules, "classify_checks", all_real)
    misses = 0
    for index, spec in enumerate(SCENARIOS):
        got = _read(spec, tmp_path / str(index)).model_dump()
        misses += got["reds"] != spec["expected"]["reading"]["reds"]
    assert misses > 0


@pytest.mark.parametrize("index", range(len(SCENARIOS)))
def test_claim_check_matches_the_old_reader(index: int, tmp_path: Path) -> None:
    """The claim-time recheck says what ``sweep_read --claim-check`` said, and exits the same."""
    spec = SCENARIOS[index]
    paths = write_files(BASE, spec, tmp_path)
    for expected in spec["expected"]["claim_checks"]:
        loaded = HandlerMergeSweepLoadFacts().handle(
            ModelMergeSweepLoadRequest(
                state_path=str(paths["state"]),
                ledger_path=str(paths["ledger"]),
                floors_path=str(paths["floors"]),
                now=spec["now"],
                load1=1.0,
                cpus=8,
                claim_check_pr=expected["pr"],
            )
        )
        assert loaded.ok, loaded.why
        assert loaded.claim_check is not None
        checked = HandlerMergeSweepClaimCheck().handle(loaded.claim_check)
        assert (checked.line, checked.exit_code) == (
            expected["line"],
            expected["exit_code"],
        )
