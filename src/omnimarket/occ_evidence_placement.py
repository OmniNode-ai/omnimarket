# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a new ``dod_evidence`` item goes in an OCC contract (OMN-19852).

Why this module exists
----------------------
Every producer that adds an item to ``contracts/<ticket>.yaml`` used to insert
it at the END of the ``dod_evidence`` list: the companion emitter's base rows
and self-bind item, the compute oracle's merged-path row, the batch window
rebuild, and the observation effect. Two companions opened from the same base
therefore both edited the same spot, the list's tail, and whichever merged
second went ``DIRTY`` on the contract. The change-control merge lane measured
it on 2026-09-27 (STATUS 2026-09-27T14:42:22Z lane=occ-merge-now-83): moving a
companion's entries off the tail, to the head of the list, let
onex_change_control#11627 and #11562 merge on their first CI after that, where
every tail append had collided with every later one.

The head alone would only move the collision: every producer inserting at the
head edits the line after ``dod_evidence:``, so two companions still conflict
with each other. So the slot is keyed by the new item's id: with ``n`` items
already declared, the new block goes before item ``k = H(id) mod n``. It is
never the tail, where any remaining tail appender writes; with one declared
item it is the head. Two companions for different PRs (their ids carry the PR
number) land in different slots unless their ids share a slot, which happens
with probability ``1/n``, and insertions separated by at least one existing
item merge cleanly under git's three-way merge. The rule is a pure function of
the contract text and the new id, so re-minting the same companion on the same
base writes the same bytes.

What does not change
--------------------
The gates key on ids, never on positions: ``validator_occ_append_only`` checks
that every base item survives with an identical per-entry hash, and a receipt's
``contract_entry_sha256`` hashes its own item. Existing bytes are untouched;
the insertion is one contiguous block, re-indented to the list's own item
indent (OMN-13888), so the diff stays one hunk. A per-entry file would isolate
entries completely, but the receipt gate reads the one contract file, so it
would need a gate change; this placement needs none.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Sequence

import yaml

DOD_EVIDENCE_KEY_RE = re.compile(r"^dod_evidence:[ \t]*$")
_ITEM_RE = re.compile(r"^([ \t]*)- ")
RENDERED_ITEM_INDENT = "  "


def _reindent_item_block(block: str, indent: str) -> str:
    """Re-indent a 2-space-rendered ``dod_evidence`` item block to ``indent``."""
    if indent == RENDERED_ITEM_INDENT:
        return block
    out: list[str] = []
    for line in block.splitlines(keepends=True):
        if not line.strip():
            out.append(line)
            continue
        body = (
            line[len(RENDERED_ITEM_INDENT) :]
            if line.startswith(RENDERED_ITEM_INDENT)
            else line
        )
        out.append(indent + body)
    return "".join(out)


def first_block_id(blocks: Sequence[str]) -> str:
    """The id of the first item the blocks declare, else the first block's text."""
    for block in blocks:
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        for item in parsed if isinstance(parsed, list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                return str(item["id"])
    return blocks[0] if blocks else ""


def evidence_slot(new_id: str, declared: int) -> int:
    """The index of the existing item the new block goes before, or ``declared``
    (the end of the list) when the list is empty. Never the tail otherwise."""
    if declared <= 0:
        return 0
    digest = hashlib.sha256(new_id.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % declared


def insert_dod_evidence_blocks(
    contract_text: str,
    blocks: Sequence[str],
    *,
    missing_key_error: type[Exception] = ValueError,
    missing_key_message: str = (
        "cannot insert dod_evidence item: contract has no block-style "
        "'dod_evidence:' key (OMN-14741 F-04)"
    ),
) -> str:
    """Insert item ``blocks`` into the contract's ``dod_evidence`` list, as one
    contiguous block, in the slot :func:`evidence_slot` picks for the first new
    id. Every existing byte is kept, in order.
    """
    if not blocks:
        return contract_text
    lines = contract_text.splitlines(keepends=True)
    key_idx: int | None = None
    for i, line in enumerate(lines):
        if DOD_EVIDENCE_KEY_RE.match(line.rstrip("\n")):
            key_idx = i
            break
    if key_idx is None:
        raise missing_key_error(missing_key_message)
    end = len(lines)
    indent: str | None = None
    item_starts: list[int] = []
    for j in range(key_idx + 1, len(lines)):
        stripped = lines[j].rstrip("\n")
        if stripped and not stripped[0].isspace() and not stripped.startswith("- "):
            end = j
            break
        match = _ITEM_RE.match(stripped)
        if match is None:
            continue
        if indent is None:
            indent = match.group(1)
        if match.group(1) == indent:
            item_starts.append(j)
    item_indent = indent if indent is not None else RENDERED_ITEM_INDENT
    slot = evidence_slot(first_block_id(blocks), len(item_starts))
    at = item_starts[slot] if slot < len(item_starts) else end
    if at == end and end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] = lines[end - 1] + "\n"
    # Every block is newline-terminated: a block lifted off the end of a contract
    # that lacked a final newline would otherwise leave the merged contract
    # unterminated (OCC yamlfmt rewrites it), or glue onto the next item.
    rendered = [
        _reindent_item_block(
            block if block.endswith("\n") else block + "\n", item_indent
        )
        for block in blocks
    ]
    return "".join(lines[:at]) + "".join(rendered) + "".join(lines[at:])


def only_inserts(before: str, after: str) -> bool:
    """True when ``after`` is ``before`` with whole lines added and none edited,
    removed or reordered: every line of ``before`` survives, in order.

    This is the add-only property a merged contract's guard needs. The earlier
    guard required ``before`` to be a byte PREFIX of ``after``, which held only
    while every producer appended at the list's tail (OMN-15485); an item
    inserted in an id-keyed slot keeps every merged byte but not as a prefix.
    """
    matcher = difflib.SequenceMatcher(
        None,
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        autojunk=False,
    )
    return all(tag in ("equal", "insert") for tag, *_ in matcher.get_opcodes())


__all__ = [
    "DOD_EVIDENCE_KEY_RE",
    "RENDERED_ITEM_INDENT",
    "evidence_slot",
    "first_block_id",
    "insert_dod_evidence_blocks",
    "only_inserts",
]
