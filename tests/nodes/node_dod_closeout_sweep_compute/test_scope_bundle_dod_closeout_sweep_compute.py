# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20655: the sweep's scope ranking lives in the node, as overlay data.

The closer's selection ranked 2,119 deferred tickets with no regard for whether a ticket had a
contract, so each run spent its slots on tickets dod_verify could not run for and flipped none
for 43 runs. These cases pin the rules that replace that: the rank rules are data in
overlay.yaml, a ticket with a contract on a default branch ranks before one without, a reserve
keeps the contractless tickets from starving, and a ticket with no contract reads "no contract".
Golden chain: the registered contract on the in-memory bus. Error chain: refusals.
Parity: with the script's own rules the bundle equals the recorded output of the code it replaces.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    GitCloneContractIndex,
    InMemoryContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    ScopeInputUnreadableError,
    bundle_text,
    rank_key,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    EnumRankRule,
    ModelCloseoutScopeOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
PARITY = Path(__file__).parent / "fixtures" / "scope_bundle_legacy_parity.json"
HANDLER = HandlerDodCloseoutSweep()
STAMP = "2026-10-01T00:00:00.000Z"


def _overlay(**changes: Any) -> ModelCloseoutScopeOverlay:
    return load_scope_overlay().model_copy(update=changes)


def _row(n: int, state: str = "In Review", at: str = STAMP) -> dict[str, Any]:
    return {
        "id": f"OMN-{n}",
        "state": state,
        "sources": ["started"],
        "last_closeout_comment_at": at,
    }


def _scope(n: int) -> tuple[list[Any], list[Any], list[Any]]:
    ids = [f"OMN-{i}" for i in range(1, n + 1)]
    return (
        [_row(i) for i in range(1, n + 1)],
        [{"id": i, "merged": ["omnimarket#1"], "open": [], "error": ""} for i in ids],
        [],
    )


def _bundle(
    n: int, contracts: set[str] | None, limit: int, overlay: ModelCloseoutScopeOverlay
) -> dict[str, Any]:
    cands, prs, texts = _scope(n)
    state = (
        None
        if contracts is None
        else [{"id": c["id"], "has_contract": c["id"] in contracts} for c in cands]
    )
    return json.loads(bundle_text(cands, prs, texts, state, limit, overlay))


def _selected(bundle: dict[str, Any]) -> list[str]:
    return [c["id"] for c in bundle["candidates"]]


# --- the overlay is the one place the rules are written -------------------------------


def test_the_overlay_declares_the_rank_rules_and_every_cap() -> None:
    overlay = load_scope_overlay()
    assert overlay.rank_rules == [
        EnumRankRule.WAITING_ON,
        EnumRankRule.HAS_CONTRACT,
        EnumRankRule.MERGED_IN_REVIEW,
    ]
    assert overlay.no_contract_reserve == 2
    assert overlay.contract_repos == []
    assert (overlay.bundle_max_bytes, overlay.held_cap, overlay.waiting_cap) == (
        16384,
        200,
        40,
    )
    assert overlay.eligible_states == ["in progress", "in review", "backlog"]


def test_an_overlay_that_drops_a_key_is_refused_never_defaulted(tmp_path: Path) -> None:
    text = (NODE_DIR / "overlay.yaml").read_text(encoding="utf-8")
    broken = tmp_path / "overlay.yaml"
    broken.write_text(text.replace("no_contract_reserve: 2\n", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="no_contract_reserve"):
        load_scope_overlay(broken)


# --- the ranking ------------------------------------------------------------------------


def test_a_ticket_with_a_contract_ranks_before_one_without() -> None:
    # 30 tickets, only 5 have contracts; the oldest 12 would otherwise be taken in id order.
    contracts = {f"OMN-{i}" for i in (20, 21, 22, 23, 24)}
    chosen = _selected(_bundle(30, contracts, 12, _overlay(no_contract_reserve=0)))
    assert contracts <= set(chosen)
    assert len(chosen) == 12


def test_failing_control_without_the_contract_rule_the_contractless_fill_the_run() -> (
    None
):
    """Named injected break: the overlay without has_contract takes the first 12 by id, none
    of the 5 tickets that can reach a verdict (the 2026-11-08 sweep: 9 of 12 had no contract)."""
    contracts = {f"OMN-{i}" for i in (20, 21, 22, 23, 24)}
    broken = _overlay(
        rank_rules=[EnumRankRule.WAITING_ON, EnumRankRule.MERGED_IN_REVIEW],
        no_contract_reserve=0,
    )
    chosen = _selected(_bundle(30, contracts, 12, broken))
    assert not (contracts & set(chosen))


def test_the_reserve_keeps_slots_for_the_oldest_contractless_tickets() -> None:
    contracts = {f"OMN-{i}" for i in range(10, 31)}  # 21 tickets with a contract
    chosen = _selected(_bundle(30, contracts, 12, load_scope_overlay()))
    without = [i for i in chosen if i not in contracts]
    assert without == ["OMN-1", "OMN-2"]
    assert len(chosen) == 12


def test_failing_control_reserve_zero_starves_the_contractless() -> None:
    contracts = {f"OMN-{i}" for i in range(10, 31)}
    chosen = _selected(_bundle(30, contracts, 12, _overlay(no_contract_reserve=0)))
    assert [i for i in chosen if i not in contracts] == []


def test_waiting_on_still_ranks_first_whatever_its_contract() -> None:
    cands, prs, _ = _scope(5)
    texts = [{"id": "OMN-5", "waiting_on": ["release cut"]}]
    state = [{"id": c["id"], "has_contract": c["id"] != "OMN-5"} for c in cands]
    bundle = json.loads(bundle_text(cands, prs, texts, state, 3, load_scope_overlay()))
    assert _selected(bundle)[0] == "OMN-5"


def test_a_scope_with_no_contract_reading_ranks_as_the_script_did() -> None:
    plain = _bundle(30, None, 12, load_scope_overlay())
    assert (
        _selected(plain) == sorted(f"OMN-{i}" for i in range(1, 31))[:12]
    )  # ids order as text
    assert plain["no_contract"] == []


def test_rank_key_follows_the_overlay_order() -> None:
    row = {**_row(1), "has_contract": False, "waiting_on": [], "sources": ["merged"]}
    first = _overlay(
        rank_rules=[EnumRankRule.HAS_CONTRACT, EnumRankRule.MERGED_IN_REVIEW]
    )
    second = _overlay(
        rank_rules=[EnumRankRule.MERGED_IN_REVIEW, EnumRankRule.HAS_CONTRACT]
    )
    assert rank_key(row, first)[:2] == (1, 0)
    assert rank_key(row, second)[:2] == (0, 1)


# --- parity with the code this replaces -------------------------------------------------


@pytest.mark.parametrize("limit", ["12", "24"])
def test_parity_with_the_recorded_output_of_the_replaced_ranking(limit: str) -> None:
    """With the script's own rules the bundle equals what the replaced code printed for the
    same 120-ticket scope (recorded once, so this holds without that code present)."""
    fixture = json.loads(PARITY.read_text(encoding="utf-8"))
    script_rules = _overlay(
        rank_rules=[EnumRankRule.WAITING_ON, EnumRankRule.MERGED_IN_REVIEW],
        no_contract_reserve=0,
    )
    text = bundle_text(
        fixture["candidates"],
        fixture["pr_state"],
        fixture["text_state"],
        None,
        int(limit),
        script_rules,
    )
    got = json.loads(text)
    got.pop("no_contract")
    assert got == fixture["legacy_bundles"][limit]


def test_failing_control_parity_breaks_when_the_ranking_changes() -> None:
    fixture = json.loads(PARITY.read_text(encoding="utf-8"))
    state = [
        {"id": c["id"], "has_contract": int(c["id"][4:]) % 2 == 0}
        for c in fixture["candidates"]
    ]
    text = bundle_text(
        fixture["candidates"], fixture["pr_state"], fixture["text_state"], state, 12,
        _overlay(no_contract_reserve=0),
    )  # fmt: skip
    got = json.loads(text)
    got.pop("no_contract")
    assert got != fixture["legacy_bundles"]["12"]


def test_the_bundle_stays_under_the_overlay_byte_cap() -> None:
    cands = [_row(i) | {"title": "x" * 80} for i in range(1, 3001)]
    prs = [
        {
            "id": c["id"],
            "merged": ["omnimarket#1"] if i < 2000 else [],
            "open": [],
            "error": "",
        }
        for i, c in enumerate(cands)
    ]
    texts = [
        {
            "id": c["id"],
            "text_sha": "0123456789abcdef",
            "held_text_sha": "0123456789abcdef",
        }
        for c in cands
    ]
    overlay = load_scope_overlay()
    text = bundle_text(cands, prs, texts, None, 12, overlay)
    assert len(text.encode("utf-8")) <= overlay.bundle_max_bytes
    bundle = json.loads(text)
    assert bundle["held_count"] == 2000
    assert bundle["waiting_count"] == 1000


# --- the contract index -------------------------------------------------------------------


def _clone(home: Path, repo: str, ids: list[str]) -> None:
    clone = home / repo
    clone.mkdir(parents=True, exist_ok=True)

    def git(*a: str) -> None:
        subprocess.run(
            ["git", "-C", str(clone), *a],
            check=True,
            capture_output=True,
            text=True,
            env=scrub_git_location_env(os.environ),
        )

    git("init", "-q", "-b", "dev")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (clone / "contracts").mkdir()
    for i in ids:
        (clone / "contracts" / f"{i}.yaml").write_text(
            f"ticket_id: {i}\n", encoding="utf-8"
        )
    (clone / "contracts" / "notes.txt").write_text("x", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")


def test_the_clone_index_reads_contracts_on_each_default_branch(tmp_path: Path) -> None:
    home = tmp_path / "registry"
    home.mkdir()
    _clone(home, "repo_a", ["OMN-1", "OMN-2"])
    _clone(home, "repo_b", ["OMN-3"])
    _clone(tmp_path, "beside_repo", ["OMN-4"])  # outside the registry root
    index = GitCloneContractIndex(
        home, load_scope_overlay(), [tmp_path / "beside_repo"]
    )
    assert index.contract_ids() == {"OMN-1", "OMN-2", "OMN-3", "OMN-4"}


def test_failing_control_a_directory_that_is_not_a_clone_is_not_read(
    tmp_path: Path,
) -> None:
    home = tmp_path / "registry"
    home.mkdir()
    _clone(home, "repo_a", ["OMN-1"])
    (home / "scratch" / "contracts").mkdir(parents=True)
    (home / "scratch" / "contracts" / "OMN-9.yaml").write_text("x", encoding="utf-8")
    assert GitCloneContractIndex(home, load_scope_overlay()).contract_ids() == {"OMN-1"}


def test_an_overlay_that_names_clones_restricts_the_search_to_them(
    tmp_path: Path,
) -> None:
    home = tmp_path / "registry"
    home.mkdir()
    _clone(home, "repo_a", ["OMN-1"])
    _clone(home, "repo_b", ["OMN-3"])
    named = _overlay(contract_repos=["repo_b"])
    assert GitCloneContractIndex(home, named).contract_ids() == {"OMN-3"}


def test_the_in_memory_index_is_the_default() -> None:
    assert InMemoryContractIndex().contract_ids() == frozenset()
    assert InMemoryContractIndex(frozenset({"OMN-1"})).contract_ids() == {"OMN-1"}


# --- "no contract" ---------------------------------------------------------------------


def _decide(detail: str) -> ModelDodCloseoutDecisionResult:
    return HANDLER.handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "decide_ticket",
                "date": "2026-11-08",
                "ticket_id": "OMN-7",
                "run_key": "2026-11-08T0745Z",
                "verify_detail": detail,
                "acs": [
                    {
                        "label": "AC1",
                        "proposed": True,
                        "accepted": True,
                        "proposed_by": "binder",
                        "accepted_by": "acceptor",
                    }
                ],
            }
        )
    )


def test_a_ticket_with_no_contract_reads_no_contract() -> None:
    result = _decide(
        "contract unresolved: no contracts/OMN-7.yaml on the default branch of any product repository"
    )
    reasons = [u.reason for u in result.ticket_unmet or []]
    assert reasons[0].startswith("no contract")
    assert not any("has no verdict" in r for r in reasons)
    assert result.decision is not None
    assert result.decision.value == "open"


def test_failing_control_a_verifier_that_did_not_run_for_another_reason_keeps_its_reading() -> (
    None
):
    result = _decide("node_dod_verify did not finish in 2400s")
    reasons = [u.reason for u in result.ticket_unmet or []]
    assert (
        reasons[0]
        == "dod_verify did not run (VERIFY_COULD_NOT_RUN), so the bound check has no verdict"
    )
    assert not reasons[0].startswith("no contract")


# --- golden chain and error chain over the bus -------------------------------------------


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_scope_bundle_ranks_a_contract_ticket_first_over_the_bus(
    tmp_path: Path,
) -> None:
    cands, prs, texts = _scope(30)
    state = [{"id": c["id"], "has_contract": c["id"] == "OMN-27"} for c in cands]
    runtime = await _run(
        tmp_path,
        {
            "kind": "scope_bundle",
            "date": "2026-11-08",
            "scope_candidates": cands,
            "scope_pr_state": prs,
            "scope_text_state": texts,
            "scope_contract_state": state,
            "max_candidates": 12,
        },
    )
    result = runtime.handler_result
    assert isinstance(result, ModelDodCloseoutDecisionResult)
    assert result.scope_bundle is not None
    bundle: dict[str, Any] = dict(result.scope_bundle)
    selected = [c["id"] for c in bundle["candidates"]]
    assert "OMN-27" in selected
    assert bundle["totals"]["selected"] == 12


@pytest.mark.asyncio
async def test_error_chain_scope_bundle_without_its_pr_state_fails_over_the_bus(
    tmp_path: Path,
) -> None:
    cands, _, texts = _scope(3)
    runtime = await _run(
        tmp_path,
        {
            "kind": "scope_bundle",
            "date": "2026-11-08",
            "scope_candidates": cands,
            "scope_text_state": texts,
            "max_candidates": 12,
        },
    )
    assert runtime.handler_result is None


def test_error_chain_scope_bundle_refuses_a_field_it_does_not_take() -> None:
    with pytest.raises(ValueError, match="scope_bundle does not take"):
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "scope_bundle",
                "date": "2026-11-08",
                "scope_candidates": [],
                "scope_pr_state": [],
                "scope_text_state": [],
                "max_candidates": 1,
                "records": [],
            }
        )


def test_error_chain_a_bundle_over_the_byte_cap_with_nothing_to_shrink_is_refused() -> (
    None
):
    cands = [_row(i) | {"title": "x" * 4000} for i in range(1, 12)]
    prs = [
        {"id": c["id"], "merged": ["omnimarket#1"], "open": [], "error": ""}
        for c in cands
    ]
    with pytest.raises(ScopeInputUnreadableError, match="lower --max"):
        bundle_text(cands, prs, [], None, 11, load_scope_overlay())


# --- the command the sweep runs ---------------------------------------------------------


def _write(path: Path, rows: object) -> Path:
    path.write_text(
        rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8"
    )
    return path


def _cli(
    tmp_path: Path, candidates: object, prs: object, texts: object, *extra: str
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable, "-m", "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep", "bundle",
        "--candidates", str(_write(tmp_path / "c.json", candidates)),
        "--pr-state", str(_write(tmp_path / "p.json", prs)),
        "--text-state", str(_write(tmp_path / "t.json", texts)),
        "--max", "12", *extra,
    ]  # fmt: skip
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_the_bundle_command_prints_one_object_with_the_contract_ranking(
    tmp_path: Path,
) -> None:
    cands, prs, texts = _scope(30)
    state = [{"id": c["id"], "has_contract": c["id"] == "OMN-30"} for c in cands]
    run = _cli(
        tmp_path,
        cands,
        prs,
        texts,
        "--contract-state",
        str(_write(tmp_path / "k.json", state)),
    )
    assert run.returncode == 0, run.stderr
    bundle = json.loads(run.stdout)
    assert "OMN-30" in [c["id"] for c in bundle["candidates"]]
    assert bundle["totals"]["selected"] == 12


def test_the_bundle_command_with_no_contract_state_reads_the_clones(
    tmp_path: Path,
) -> None:
    home = tmp_path / "registry"
    home.mkdir()
    _clone(home, "repo_a", ["OMN-30"])
    cands, prs, texts = _scope(30)
    run = subprocess.run(
        [
            sys.executable, "-m", "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep", "bundle",
            "--candidates", str(_write(tmp_path / "c.json", cands)),
            "--pr-state", str(_write(tmp_path / "p.json", prs)),
            "--text-state", str(_write(tmp_path / "t.json", texts)),
            "--max", "12",
        ],
        capture_output=True, text=True, check=False, env={"OMNI_HOME": str(home), "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    bundle = json.loads(run.stdout)
    assert "OMN-30" in [c["id"] for c in bundle["candidates"]]
    assert "OMN-30" not in bundle["no_contract"]
    assert len(bundle["no_contract"]) == 11


def test_error_chain_the_command_refuses_garbled_input_and_never_prints_an_empty_bundle(
    tmp_path: Path,
) -> None:
    run = _cli(
        tmp_path,
        "not json",
        [],
        [],
        "--contract-state",
        str(_write(tmp_path / "k.json", [])),
    )
    assert run.returncode == 2
    assert "REFUSED" in run.stderr
    assert "not JSON" in run.stderr
    assert run.stdout == ""
