# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the reader node reads a sweep as the script reads it, from the watcher state (OMN-21069).

``fixtures/recorded_sweep.json`` was recorded by running the merge-sweep skill's own
``sweep_read.live_reading``, ``plan`` and ``claim_check`` (``recorded_from`` names the script) over
a trimmed snapshot of the PR watcher's state file, with every read served from the scenario (no
network): one scenario is the snapshot whole, the others are variants (drafts, run overrides, check
detail the state lacks, changed files, merges, ledger rows, controller ticks, chains, out-of-scope
rulings, self-lane claim checks). The expected reading and plan in each scenario are the script's
own. The chain under test is the production one: the effect node loads the files, the compute node
reads. The repositories reach the node as data on the request, as the script read them.

The node's answers equal the script's after these normalizations only, each named where it applies:
a red's ``head`` and ``armed`` (the effect carries neither yet) and the node's ``ready_at`` are
projected out of the reds; the plan's ``out-of-scope-repo`` skips of the PRs the reader set aside
are compared once the plan compute lists them (it does not yet).
"""

from __future__ import annotations

import random
import re
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

from .sweep_scenarios import load_recorded, repository_facts, write_files

RECORDED = load_recorded()
BASE = RECORDED["base"]
SCENARIOS = RECORDED["scenarios"]
BY_ID = {s["id"]: s for s in SCENARIOS}
READING_KEYS = (
    "product",
    "controller",
    "chain_heads",
    "escalations",
    "out_of_scope_prs",
)
# A red's fields the node does not carry yet (the effect reads neither head nor armed), and the
# node's own ready_for_review time, which the script never reads.
RED_ONLY_ONE_SIDE = ("head", "armed", "ready_at")


def _reds(reds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in red.items() if k not in RED_ONLY_ONE_SIDE} for red in reds
    ]


def _skipped(plan: dict[str, Any], out_of_scope_prs: list[str]) -> list[dict[str, Any]]:
    """The plan's skips without the reader's set-aside PRs, which the plan compute lists later."""
    return [
        x
        for x in plan["skipped"]
        if not (x["why"] == "out-of-scope-repo" and x["pr"] in out_of_scope_prs)
    ]


def _load(spec: dict[str, Any], root: Path, **extra: Any) -> Any:
    paths = write_files(BASE, spec, root)
    return HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(paths["state"]),
            ledger_path=str(paths["ledger"]),
            ticks_path=str(paths["ticks"]) if "ticks" in paths else None,
            floors_path=str(paths["floors"]),
            now=spec["now"],
            load1=spec["load1"],
            cpus=spec["cpus"],
            window_min=spec["window_min"],
            **repository_facts(RECORDED, spec),
            **extra,
        )
    )


def _facts(spec: dict[str, Any], root: Path) -> ModelMergeSweepReadRequest:
    """Load the scenario's files through the effect node, then add what the state lacks."""
    loaded = _load(spec, root)
    assert loaded.ok, loaded.why
    assert loaded.facts is not None
    facts = loaded.facts
    # The watcher records neither a PR's changed files nor a merge's changed files, and the effect
    # does not yet say what the state answered for a PR's checks; the scenario supplies them the way
    # the script read them.
    files, checks = spec.get("files", {}), spec.get("checks_state", {})
    open_prs = [
        p.model_copy(
            update={
                "files": files.get(f"{p.repo}#{p.number}", []),
                "facts_unread": [],
                **(
                    {"checks_state": checks[f"{p.repo}#{p.number}"]}
                    if f"{p.repo}#{p.number}" in checks
                    else {}
                ),
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
    # The script never reads a ready_for_review time, so it never assigns draft-era.
    assert classes >= {
        "real",
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
    assert any(r["out_of_scope_prs"] for r in readings)
    assert any(r["product"]["excluded"]["docs_only"] for r in readings)
    assert any(r["product"]["excluded"]["files_unread"] for r in readings)
    assert any(r["product"]["excluded"]["non_product_repo"] for r in readings)
    assert any(r["controller"]["stalled"] for r in readings)
    assert any(not r["controller"]["stalled"] for r in readings)
    assert any(red["classes"] is None for r in readings for red in r["reds"])
    assert any(
        c.get("self_lane") for s in SCENARIOS for c in s["expected"]["claim_checks"]
    )
    kinds = {lane["kind"] for s in SCENARIOS for lane in s["expected"]["plan"]["lanes"]}
    assert kinds == {"diagnose", "escalation", "land-chain-head", "fix"}


def test_no_repository_is_named_in_the_fixture() -> None:
    """Every repository the fixture reads is a placeholder; the names arrive as data."""
    repos = RECORDED["repositories"]
    named = set(repos["fleet"]) | set(repos["non_product"]) | set(BASE["repos"])
    for spec in SCENARIOS:
        named |= set(spec.get("out_of_scope", {})) | set(spec["floors"])
        named |= {x["repo"] for x in spec.get("extra_prs", [])}
    named |= {rec["facts"]["repo"] for rec in BASE["prs"].values()}
    assert named
    assert all(re.fullmatch(r"repo-\d\d", name) for name in named), sorted(named)


@pytest.mark.parametrize("index", range(len(SCENARIOS)))
def test_reading_matches_the_old_reader(index: int, tmp_path: Path) -> None:
    spec = SCENARIOS[index]
    got = _read(spec, tmp_path).model_dump()
    expected = spec["expected"]["reading"]
    assert {k: got[k] for k in READING_KEYS} == {k: expected[k] for k in READING_KEYS}
    assert _reds(got["reds"]) == _reds(expected["reds"])
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
    set_aside = spec["expected"]["reading"]["out_of_scope_prs"]
    assert {k: got[k] for k in ("lab_only", "lanes")} == {
        k: expected[k] for k in ("lab_only", "lanes")
    }
    assert _skipped(got, set_aside) == _skipped(expected, set_aside)
    assert got["load_per_core"] == pytest.approx(expected["load_per_core"])


def test_out_of_scope_prs_are_named_and_never_read(tmp_path: Path) -> None:
    """A ruled-out repository's PRs are named; none is classified, owned or planned."""
    spec = BY_ID["wu1-out-of-scope-snapshot"]
    (ruled_out,) = spec["out_of_scope"]
    got = _read(spec, tmp_path)
    assert got.out_of_scope_prs == spec["expected"]["reading"]["out_of_scope_prs"]
    assert len(got.out_of_scope_prs) == sum(
        rec["facts"]["repo"] == ruled_out for rec in BASE["prs"].values()
    )
    assert all(key.startswith(f"{ruled_out}#") for key in got.out_of_scope_prs)
    assert not [red for red in got.reds if red["repo"] == ruled_out]
    assert ruled_out not in got.scope
    unruled = _facts(spec, tmp_path / "unruled").model_copy(update={"out_of_scope": {}})
    again = HandlerMergeSweepRead().handle(unruled)
    assert [red for red in again.reds if red["repo"] == ruled_out]


def test_every_red_is_classified(tmp_path: Path) -> None:
    """No cap: 45 reds are 45 classified reds, in (repository, number) order."""
    spec = BY_ID["wu1-every-red-classified"]
    got = _read(spec, tmp_path)
    assert len(got.reds) == 45
    assert all(red["classes"] for red in got.reds)
    keys = [(red["repo"], red["number"]) for red in got.reds]
    assert keys == sorted(keys)
    assert _reds(got.model_dump()["reds"]) == _reds(spec["expected"]["reading"]["reds"])


def test_shuffled_input_reads_the_same(tmp_path: Path) -> None:
    """The order the PRs arrive in does not change the reading."""
    spec = BY_ID["wu1-every-red-classified"]
    facts = _facts(spec, tmp_path)
    in_order = HandlerMergeSweepRead().handle(facts)
    for seed in range(3):
        shuffled = list(facts.open_prs)
        random.Random(seed).shuffle(shuffled)
        got = HandlerMergeSweepRead().handle(
            facts.model_copy(update={"open_prs": shuffled})
        )
        assert got == in_order


def test_a_rollup_red_without_detail_keeps_its_red_with_classes_unread(
    tmp_path: Path,
) -> None:
    spec = BY_ID["wu1-rollup-only"]
    (key,) = [k for k, v in spec["checks_state"].items() if v == "rollup-only"]
    got = _read(spec, tmp_path)
    (red,) = [r for r in got.reds if f"{r['repo']}#{r['number']}" == key]
    assert red["classes"] is None
    assert f"checks:{key}" in {u.field for u in got.unread}


def test_merges_count_only_in_the_fleet_and_non_product_repositories(
    tmp_path: Path,
) -> None:
    spec = BY_ID["wu1-merges-by-repository"]
    got = _read(spec, tmp_path)
    assert got.product == spec["expected"]["reading"]["product"]
    assert got.product["all"] < len(spec["merges"])
    assert got.product["excluded"]["non_product_repo"] == 4


def test_an_unread_fact_is_listed_and_never_a_zero(tmp_path: Path) -> None:
    """What the watcher does not record is named, on the state as loaded."""
    spec = SCENARIOS[0]
    loaded = _load(spec, tmp_path)
    assert loaded.facts is not None
    result = HandlerMergeSweepRead().handle(
        loaded.facts.model_copy(update={"ticks": None})
    )
    fields = {u.field for u in result.unread}
    assert "controller" in fields
    # Unread merge files are counted once, under product.excluded.files_unread.
    assert "merge-files" not in fields
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
    for n, expected in enumerate(spec["expected"]["claim_checks"]):
        loaded = _load(spec, tmp_path / str(n), claim_check_pr=expected["pr"])
        assert loaded.ok, loaded.why
        assert loaded.claim_check is not None
        asked = loaded.claim_check.model_copy(
            update={"self_lane": expected.get("self_lane")}
        )
        checked = HandlerMergeSweepClaimCheck().handle(asked)
        assert (checked.line, checked.exit_code) == (
            expected["line"],
            expected["exit_code"],
        )


def test_a_lanes_own_claim_is_not_an_owner(tmp_path: Path) -> None:
    """Exit 0 for the asking lane's own CLAIM, 2 for another live lane's."""
    spec = BY_ID["wu1-self-lane-claim-check"]
    codes = {}
    for n, expected in enumerate(spec["expected"]["claim_checks"]):
        loaded = _load(spec, tmp_path / str(n), claim_check_pr=expected["pr"])
        assert loaded.claim_check is not None
        asked = loaded.claim_check.model_copy(
            update={"self_lane": expected.get("self_lane")}
        )
        codes[(expected["pr"], expected.get("self_lane"))] = (
            HandlerMergeSweepClaimCheck().handle(asked).exit_code
        )
    own, peer = sorted({pr for pr, _ in codes})
    assert codes[(own, "sweep-self")] == 0
    assert codes[(peer, "sweep-self")] == 2
    assert codes[(own, None)] == 2
