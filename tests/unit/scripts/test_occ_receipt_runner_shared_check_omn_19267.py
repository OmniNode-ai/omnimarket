# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19267 — one declared check, one supersession record, per cohort.

OCC's Receipt Hardening Gate rule S1 (OMN-15459) refuses two supersessions in
one cohort (ticket directory plus ``.supersede.<TOKEN>.`` suffix) whose
whitespace-normalised ``replacement.check_value`` is byte-identical while they
name different evidence items. The runner filed one record per item, so a
contract whose items declare the same check produced a pair S1 rejects. The
OMN-19267 contract has exactly that pair:
``dod-b01-19267-ac2-reasoning-profiles-no-think`` and its second-lane
acceptance ``dod-accept-d-19267-ac2`` both run
``test_prose_task_class_no_think_omn18967.py``. Once pushed, nothing repairs it:
deleting a record trips the Append-Only Gate and a repair record trips
SUPERSESSION_CHAIN (onex_change_control#12821, then #12855).

What this module pins:

* a shared check is executed once and filed once, under one item, and the
  cohort the runner leaves behind satisfies the S1 predicate;
* the other item keeps its standing PASS, and the run says so;
* the record goes to the item that needs it, and stays on the same item from
  one head to the next;
* an item that shares the check and does NOT stand is a refusal that fails
  the run, never a second record and never a silent skip;
* a record already in the cohort for another item is never matched by a new
  one;
* items with distinct checks, and keys with no base receipt, behave as before.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

runner = importlib.import_module("occ_receipt_runner")

TICKET = "OMN-19267"
FIRST = "dod-b01-19267-ac2-reasoning-profiles-no-think"
SECOND = "dod-accept-d-19267-ac2"
PR_NUMBER = 3401
HEAD_SHA = "c" * 40
NEXT_SHA = "d" * 40
OLD_SHA = "e" * 40
BRANCH = "jonah/omn-19267-hosted-ac1-ac2-bindings"
REPO = "OmniNode-ai/omnimarket"

SHARED_CHECK = "printf '3 passed in 0.42s\\n'"
# The same check as YAML folding or a hand edit may space it. S1 collapses
# whitespace before comparing, so the runner must treat it as the same check.
SHARED_CHECK_RESPACED = "printf  '3 passed in 0.42s\\n'"
OTHER_CHECK = "printf '5 passed in 0.10s\\n'"


def _item(item_id: str, check_value: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "description": f"AC2 behaviour proof ({item_id})",
        "source": "generated",
        "checks": [{"check_type": "test_passes", "check_value": check_value}],
    }


def _contract(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ticket_id": TICKET, "title": "reasoning classes", "dod_evidence": items}


def _occ_root(tmp_path: Path, items: list[dict[str, Any]]) -> Path:
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True, exist_ok=True)
    (root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(_contract(items), sort_keys=True), encoding="utf-8"
    )
    return root


def _ticket_dir(occ_root: Path) -> Path:
    return occ_root / "drift" / "dod_receipts" / TICKET


def _write_base(occ_root: Path, item_id: str, check_value: str, *, status: str) -> Path:
    """A base receipt bound to the item's current contract entry."""
    contract = yaml.safe_load(
        (occ_root / "contracts" / f"{TICKET}.yaml").read_text(encoding="utf-8")
    )
    path = _ticket_dir(occ_root) / item_id / "test_passes.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": TICKET,
                "evidence_item_id": item_id,
                "check_type": "test_passes",
                "check_value": check_value,
                "status": status,
                "run_timestamp": datetime(2026, 10, 3, 7, 37, 19, tzinfo=UTC),
                "commit_sha": OLD_SHA,
                "runner": "capture-lane@h200",
                "verifier": "occ-probe-capture-v1",
                "probe_command": check_value,
                "probe_stdout": "3 passed in 0.42s" if status == "PASS" else "",
                "exit_code": 0 if status == "PASS" else None,
                "pr_number": 2809,
                "contract_entry_sha256": compute_contract_entry_sha256(
                    contract, item_id
                ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _run(occ_root: Path, product_root: Path, *, head_sha: str = HEAD_SHA) -> Any:
    return runner.run(
        occ_root=occ_root,
        product_root=product_root,
        ticket_ids=(TICKET,),
        pr_number=PR_NUMBER,
        repo=REPO,
        head_sha=head_sha,
        branch=BRANCH,
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )


def _records(occ_root: Path, item_id: str) -> list[Path]:
    directory = _ticket_dir(occ_root) / item_id
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.supersede.*.yaml"))


def _s1_collisions(occ_root: Path) -> list[tuple[str, str, str]]:
    """The S1 predicate over every cohort the runner left behind.

    Groups supersession records by ``.supersede.<TOKEN>.`` within the ticket
    directory and reports any two that carry the same whitespace-normalised
    ``replacement.check_value`` under different evidence items.
    """
    by_token: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(_ticket_dir(occ_root).glob("*/*.supersede.*.yaml")):
        token = path.name.split(".supersede.", 1)[1].rsplit(".yaml", 1)[0]
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        value = " ".join(str(raw["replacement"]["check_value"]).split())
        by_token.setdefault(token, []).append((raw["evidence_item_id"], value))
    collisions: list[tuple[str, str, str]] = []
    for token, members in by_token.items():
        for index, (item, value) in enumerate(members):
            for other_item, other_value in members[index + 1 :]:
                if value == other_value and item != other_item:
                    collisions.append((token, item, other_item))
    return collisions


@pytest.mark.unit
def test_shared_check_with_two_standing_passes_is_filed_once(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    first_base = _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    second_base = _write_base(occ_root, SECOND, SHARED_CHECK, status="PASS")
    first_bytes = first_base.read_bytes()
    second_bytes = second_base.read_bytes()

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 1
    assert outcome.write_refusals == ()
    assert [path.name for path in _records(occ_root, FIRST)] == [
        f"test_passes.supersede.{PR_NUMBER}.yaml"
    ]
    assert _records(occ_root, SECOND) == []
    assert len(outcome.skipped_shared_check) == 1
    assert outcome.skipped_shared_check[0].startswith(f"{TICKET}:{SECOND}:test_passes")
    assert FIRST in outcome.skipped_shared_check[0]
    assert _s1_collisions(occ_root) == []
    # Append-only: neither base receipt was opened for write.
    assert first_base.read_bytes() == first_bytes
    assert second_base.read_bytes() == second_bytes
    assert runner.main(_argv(occ_root, tmp_path)) == 0


@pytest.mark.unit
def test_whitespace_variants_of_one_check_are_one_check(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK_RESPACED)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    _write_base(occ_root, SECOND, SHARED_CHECK_RESPACED, status="PASS")

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 1
    assert _records(occ_root, SECOND) == []
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_same_head_rerun_files_nothing_more(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    _write_base(occ_root, SECOND, SHARED_CHECK, status="PASS")
    _run(occ_root, tmp_path)

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 0
    assert outcome.wrote == ()
    assert outcome.skipped_already_pass == 1
    assert outcome.write_refusals == ()
    assert _records(occ_root, SECOND) == []
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_the_carrier_is_stable_across_heads(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    _write_base(occ_root, SECOND, SHARED_CHECK, status="PASS")
    _run(occ_root, tmp_path)

    outcome = _run(occ_root, tmp_path, head_sha=NEXT_SHA)

    assert outcome.executed == 1
    assert outcome.write_refusals == ()
    assert [path.name for path in _records(occ_root, FIRST)] == [
        f"test_passes.supersede.{PR_NUMBER}.0002.yaml",
        f"test_passes.supersede.{PR_NUMBER}.yaml",
    ]
    assert _records(occ_root, SECOND) == []
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_the_record_goes_to_the_item_that_needs_it(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    _write_base(occ_root, SECOND, SHARED_CHECK, status="PENDING")

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 1
    assert outcome.write_refusals == ()
    assert _records(occ_root, FIRST) == []
    assert [path.name for path in _records(occ_root, SECOND)] == [
        f"test_passes.supersede.{PR_NUMBER}.yaml"
    ]
    assert outcome.skipped_shared_check[0].startswith(f"{TICKET}:{FIRST}:test_passes")
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_a_sharing_item_that_does_not_stand_is_a_refusal(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PENDING")
    _write_base(occ_root, SECOND, SHARED_CHECK, status="PENDING")

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 1
    assert len(_records(occ_root, FIRST)) == 1
    assert _records(occ_root, SECOND) == []
    assert len(outcome.write_refusals) == 1
    assert outcome.write_refusals[0].startswith(f"{TICKET}:{SECOND}:test_passes")
    assert "S1" in outcome.write_refusals[0]
    assert outcome.skipped_shared_check == ()
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_refusal_fails_the_run(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PENDING")
    _write_base(occ_root, SECOND, SHARED_CHECK, status="PENDING")

    assert runner.main(_argv(occ_root, tmp_path)) == 1


@pytest.mark.unit
def test_a_stale_entry_binding_does_not_stand(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    second_base = _write_base(occ_root, SECOND, SHARED_CHECK, status="PASS")
    raw = yaml.safe_load(second_base.read_text(encoding="utf-8"))
    raw["contract_entry_sha256"] = f"sha256:{'0' * 64}"
    second_base.write_text(yaml.safe_dump(raw, sort_keys=True), encoding="utf-8")

    outcome = _run(occ_root, tmp_path)

    # The stale item needs the record, so it carries it; the standing one waits.
    assert [path.name for path in _records(occ_root, SECOND)] == [
        f"test_passes.supersede.{PR_NUMBER}.yaml"
    ]
    assert _records(occ_root, FIRST) == []
    assert outcome.write_refusals == ()
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_an_existing_cohort_record_is_never_matched(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, [_item(FIRST, SHARED_CHECK)])
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PENDING")
    bound = runner.bind_command_to_product_repo(
        SHARED_CHECK, repo=REPO, head_sha=HEAD_SHA
    )
    foreign = _ticket_dir(occ_root) / SECOND / f"test_passes.supersede.{PR_NUMBER}.yaml"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(
        yaml.safe_dump(
            {
                "evidence_item_id": SECOND,
                "supersedes": f"drift/dod_receipts/{TICKET}/{SECOND}/test_passes.yaml",
                "replacement": {"evidence_item_id": SECOND, "check_value": bound},
            }
        ),
        encoding="utf-8",
    )

    outcome = _run(occ_root, tmp_path)

    assert _records(occ_root, FIRST) == []
    assert len(outcome.write_refusals) == 1
    assert SECOND in outcome.write_refusals[0]
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_distinct_checks_each_get_their_own_record(tmp_path: Path) -> None:
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, OTHER_CHECK)]
    )
    _write_base(occ_root, FIRST, SHARED_CHECK, status="PASS")
    _write_base(occ_root, SECOND, OTHER_CHECK, status="PASS")

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 2
    assert len(_records(occ_root, FIRST)) == 1
    assert len(_records(occ_root, SECOND)) == 1
    assert outcome.skipped_shared_check == ()
    assert _s1_collisions(occ_root) == []


@pytest.mark.unit
def test_keys_without_a_base_receipt_are_not_grouped(tmp_path: Path) -> None:
    # S1 judges only supersession records. A key with no base receipt gets a
    # net-new base receipt of its own, exactly as before OMN-19267.
    occ_root = _occ_root(
        tmp_path, [_item(FIRST, SHARED_CHECK), _item(SECOND, SHARED_CHECK)]
    )

    outcome = _run(occ_root, tmp_path)

    assert outcome.executed == 2
    assert outcome.write_refusals == ()
    for item_id in (FIRST, SECOND):
        assert (_ticket_dir(occ_root) / item_id / "test_passes.yaml").is_file()


def _argv(occ_root: Path, product_root: Path) -> list[str]:
    return [
        "--occ-root",
        str(occ_root),
        "--product-root",
        str(product_root),
        "--pr-number",
        str(PR_NUMBER),
        "--repo",
        REPO,
        "--head-sha",
        HEAD_SHA,
        "--branch",
        BRANCH,
        "--run-url",
        "https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
        "--tickets",
        TICKET,
    ]
