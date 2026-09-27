# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure ticket-batch companion helpers (OMN-16336)."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    ci_check_evidence_id,
    pr_scoped_slot_evidence_id,
)

_MEMBER_BASE_RE = re.compile(r"^dod-.+-pr-\d+$")
_DOD_KEY_RE = re.compile(r"^dod_evidence:[ \t]*$")
_LIST_ITEM_RE = re.compile(r"^([ \t]*)- ")


def member_evidence_ids(*, repo: str, pr_number: int) -> frozenset[str]:
    """Return every contract evidence id owned by one product PR."""
    base = f"dod-{repo.replace('/', '-')}-pr-{pr_number}"
    return frozenset(
        {
            base,
            ci_check_evidence_id(base),
            pr_scoped_slot_evidence_id(
                BEHAVIOR_PROOF_EVIDENCE_ID, repo=repo, pr_number=pr_number
            ),
            pr_scoped_slot_evidence_id(
                ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
                repo=repo,
                pr_number=pr_number,
            ),
        }
    )


def batch_member_bases(ticket: str, receipt_paths: Iterable[str]) -> tuple[str, ...]:
    """Recover ordered unique member base ids from ticket receipt paths."""
    prefix = f"drift/dod_receipts/{ticket}/"
    excluded_prefixes = (
        BEHAVIOR_PROOF_EVIDENCE_ID,
        ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    )
    seen: set[str] = set()
    members: list[str] = []
    for path in receipt_paths:
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        evidence_id, separator, _rest = relative.partition("/")
        if not separator or not _MEMBER_BASE_RE.fullmatch(evidence_id):
            continue
        if evidence_id.startswith(excluded_prefixes) or evidence_id in seen:
            continue
        seen.add(evidence_id)
        members.append(evidence_id)
    return tuple(members)


def _dod_item_ranges(contract_text: str) -> list[tuple[int, int, str]]:
    lines = contract_text.splitlines(keepends=True)
    key_index: int | None = None
    for index, line in enumerate(lines):
        if _DOD_KEY_RE.fullmatch(line.rstrip("\r\n")):
            key_index = index
            break
    if key_index is None:
        return []

    list_end = len(lines)
    item_indent: str | None = None
    starts: list[int] = []
    for index in range(key_index + 1, len(lines)):
        stripped_newline = lines[index].rstrip("\r\n")
        if (
            stripped_newline
            and not stripped_newline[0].isspace()
            and not stripped_newline.startswith("- ")
        ):
            list_end = index
            break
        match = _LIST_ITEM_RE.match(stripped_newline)
        if match is None:
            continue
        if item_indent is None:
            item_indent = match.group(1)
        if match.group(1) == item_indent:
            starts.append(index)

    ranges: list[tuple[int, int, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else list_end
        block = "".join(lines[start:end])
        parsed = yaml.safe_load(block)
        if (
            not isinstance(parsed, list)
            or not parsed
            or not isinstance(parsed[0], dict)
        ):
            continue
        evidence_id = parsed[0].get("id")
        if isinstance(evidence_id, str):
            ranges.append((start, end, evidence_id))
    return ranges


def extract_dod_evidence_blocks(contract_text: str, ids: Collection[str]) -> list[str]:
    """Extract matching ``dod_evidence`` list items without changing bytes."""
    wanted = set(ids)
    lines = contract_text.splitlines(keepends=True)
    return [
        "".join(lines[start:end])
        for start, end, evidence_id in _dod_item_ranges(contract_text)
        if evidence_id in wanted
    ]


def remove_dod_evidence_items(contract_text: str, ids: Collection[str]) -> str:
    """Remove matching ``dod_evidence`` items and preserve all other bytes."""
    unwanted = set(ids)
    if not unwanted:
        return contract_text
    lines = contract_text.splitlines(keepends=True)
    removed: set[int] = set()
    for start, end, evidence_id in _dod_item_ranges(contract_text):
        if evidence_id in unwanted:
            removed.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in removed)


__all__ = [
    "batch_member_bases",
    "extract_dod_evidence_blocks",
    "member_evidence_ids",
    "remove_dod_evidence_items",
]
