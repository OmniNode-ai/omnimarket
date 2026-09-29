# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19852: a new dod_evidence item goes in a slot keyed by its id, never the tail.

Companions opened from the same base all appended to the END of the contract's
dod_evidence list, so whichever merged second went DIRTY on the contract (STATUS
2026-09-27T14:42:22Z lane=occ-merge-now-83). The behaviour test here is a real
three-way merge with ``git merge-file``: two companions for different PRs, minted
from one base, merge cleanly under the new placement and conflict under the old
tail append (the RED control).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.events.occ_observation_store import insert_dod_evidence_item
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    append_dod_evidence_items,
)
from omnimarket.occ_evidence_placement import (
    evidence_slot,
    insert_dod_evidence_blocks,
    only_inserts,
)

pytestmark = pytest.mark.unit


def _item(item_id: str) -> str:
    return (
        f"  - id: {item_id}\n"
        f"    description: evidence for {item_id}\n"
        "    checks:\n"
        "      - check_type: command\n"
        f"        check_value: echo {item_id}\n"
    )


def _contract(ids: list[str]) -> str:
    return (
        "---\nticket_id: OMN-19852\nsummary: placement fixture\ndod_evidence:\n"
        + "".join(_item(i) for i in ids)
        + "status: open\n"
    )


BASE_IDS = [f"dod-existing-{n}" for n in range(6)]
# Two companions' downstream items, as the emitter names them (the PR number is in the id).
COMPANION_A = "dod-occ-diff-derived-behavior-proof-pr-3007"
COMPANION_B = "dod-occ-diff-derived-behavior-proof-pr-3016"


def _ids(text: str) -> list[str]:
    return [item["id"] for item in yaml.safe_load(text)["dod_evidence"]]


def _tail_append(text: str, block: str) -> str:
    """The pre-OMN-19852 placement: the end of the dod_evidence block."""
    head, sep, rest = text.partition("status: open\n")
    return head + block + sep + rest


def _merge(tmp_path: Path, base: str, ours: str, theirs: str) -> tuple[int, str]:
    files = {"ours": ours, "base": base, "theirs": theirs}
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    proc = subprocess.run(
        ["git", "merge-file", "-p", "ours", "base", "theirs"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    return proc.returncode, proc.stdout


def test_two_companions_from_one_base_merge_cleanly(tmp_path: Path) -> None:
    base = _contract(BASE_IDS)
    assert evidence_slot(COMPANION_A, len(BASE_IDS)) != evidence_slot(
        COMPANION_B, len(BASE_IDS)
    ), "fixture ids must map to different slots"
    ours = append_dod_evidence_items(base, [_item(COMPANION_A)])
    theirs = append_dod_evidence_items(base, [_item(COMPANION_B)])
    code, merged = _merge(tmp_path, base, ours, theirs)
    assert code == 0, merged
    assert set(_ids(merged)) == {*BASE_IDS, COMPANION_A, COMPANION_B}


def test_red_control_the_old_tail_append_conflicts(tmp_path: Path) -> None:
    base = _contract(BASE_IDS)
    ours = _tail_append(base, _item(COMPANION_A))
    theirs = _tail_append(base, _item(COMPANION_B))
    code, merged = _merge(tmp_path, base, ours, theirs)
    assert code > 0, "the tail append no longer reproduces the conflict"
    assert "<<<<<<<" in merged


def test_never_the_tail_and_the_head_with_one_item() -> None:
    for n in range(1, 9):
        base = _contract(BASE_IDS[:n] if n <= 6 else [*BASE_IDS, "dod-x", "dod-y"][:n])
        out = append_dod_evidence_items(base, [_item(COMPANION_A)])
        ids = _ids(out)
        assert ids[-1] != COMPANION_A, (n, ids)
        assert ids.index(COMPANION_A) == evidence_slot(COMPANION_A, n)
    one = append_dod_evidence_items(_contract(["dod-only"]), [_item(COMPANION_A)])
    assert _ids(one) == [COMPANION_A, "dod-only"]
    empty = "---\nticket_id: OMN-19852\ndod_evidence:\nstatus: open\n"
    assert _ids(append_dod_evidence_items(empty, [_item(COMPANION_A)])) == [COMPANION_A]


def test_existing_bytes_kept_in_order_and_the_insert_is_one_block() -> None:
    base = _contract(BASE_IDS)
    blocks = [_item(COMPANION_A), _item(COMPANION_A + "-ci")]
    out = append_dod_evidence_items(base, blocks)
    inserted = "".join(blocks)
    assert inserted in out
    assert out.replace(inserted, "", 1) == base
    assert out.startswith("---\nticket_id: OMN-19852")
    assert out.endswith("status: open\n")


def test_same_inputs_write_the_same_bytes() -> None:
    base = _contract(BASE_IDS)
    assert append_dod_evidence_items(
        base, [_item(COMPANION_B)]
    ) == append_dod_evidence_items(base, [_item(COMPANION_B)])


def test_column_zero_list_is_reindented_and_placed_off_the_tail() -> None:
    base = yaml.safe_dump(yaml.safe_load(_contract(BASE_IDS[:3])), sort_keys=False)
    out = append_dod_evidence_items(base, [_item(COMPANION_A)])
    ids = _ids(out)
    assert COMPANION_A in ids, ids
    assert ids[-1] != COMPANION_A, ids


def test_all_producers_share_one_placement() -> None:
    base = _contract(BASE_IDS)
    block = _item(COMPANION_B)
    expected = insert_dod_evidence_blocks(base, [block])
    assert append_dod_evidence_items(base, [block]) == expected
    assert OccCompanionEmitter._insert_dod_evidence_items(base, [block]) == expected
    assert insert_dod_evidence_item(base, block) == expected


def test_missing_key_errors_keep_each_callers_type() -> None:
    with pytest.raises(ValueError, match="OMN-14741 F-04"):
        append_dod_evidence_items("---\nticket_id: OMN-1\n", [_item("a")])
    with pytest.raises(RuntimeError, match="no block-style"):
        insert_dod_evidence_item("---\nticket_id: OMN-1\n", _item("a"))


def test_only_inserts_accepts_added_lines_and_refuses_edits() -> None:
    base = _contract(BASE_IDS)
    assert only_inserts(base, append_dod_evidence_items(base, [_item(COMPANION_A)]))
    assert only_inserts(base, base + _item(COMPANION_A))
    # An edited or removed merged line is refused, wherever the change sits.
    assert not only_inserts(base, base.replace("evidence for dod-existing-2", "edited"))
    assert not only_inserts(base, base.replace(_item("dod-existing-4"), ""))


@pytest.mark.parametrize("block_ends_with_newline", [True, False])
@pytest.mark.parametrize("new_id", [f"dod-x-{n}" for n in range(6)])
def test_inserted_contract_always_ends_with_one_newline_omn_16336(
    new_id: str, block_ends_with_newline: bool
) -> None:
    """A block without a trailing newline never leaves the contract unterminated.

    OCC's yamlfmt hook rewrites a file with no final newline, so a window
    companion carrying one failed Pre-commit and sat behind a formatting check
    (onex_change_control#11757, 2026-09-28).
    """
    base = "---\nticket_id: OMN-1\ndod_evidence:\n" + _item("dod-base-a")
    block = _item(new_id)
    if not block_ends_with_newline:
        block = block.rstrip("\n")
    result = insert_dod_evidence_blocks(base, [block])
    assert result.endswith("\n")
    assert not result.endswith("\n\n")
    declared = {item["id"] for item in yaml.safe_load(result)["dod_evidence"]}
    assert {"dod-base-a", new_id} <= declared
