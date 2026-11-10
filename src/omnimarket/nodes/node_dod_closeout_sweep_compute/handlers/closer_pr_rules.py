# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's pull request rows, as pure rules over what a caller read (OMN-20655).

Ported from the frozen internal repository's closeout scope reader, with its list of product
repositories and their default branches moved into the node's overlay. A ticket's merged PRs
are the squash-merge subjects on each listed repository's own default branch that end in
``(#n)`` and name the ticket; its open PRs are the watcher state's OPEN PRs whose title or
head branch names it. Nothing here reads a clone or a file: ``closer_pr_sources`` does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)

_TICKET = re.compile(r"^OMN-\d+$")
_SUBJECT_ID = re.compile(r"\bOMN-(\d+)\b")
_SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")
# Branch names spell the id in lower case with or without the hyphen (omn-20655-x, omn20655).
_LOOSE_ID = re.compile(r"(?i)\bomn-?(\d+)")


def merged_from_subjects(
    repo: str, subjects: Iterable[str], overlay: ModelCloseoutScopeOverlay
) -> dict[str, list[str]]:
    """Ticket id -> ``<repo>#<n>`` for every squash-merge subject naming it.

    Only a subject ending in ``(#n)`` is a merged pull request; a direct push names no PR and
    proves nothing about review. An ``evidence(<id>):`` subject is the closer's own bindings
    PR, never the ticket's work landing.
    """
    out: dict[str, list[str]] = {}
    for subject in subjects:
        pr = _SUBJECT_PR.search(subject)
        if not pr or subject.startswith(overlay.evidence_subject_prefix):
            continue
        for number in dict.fromkeys(_SUBJECT_ID.findall(subject)):
            out.setdefault(f"OMN-{number}", []).append(f"{repo}#{pr[1]}")
    return out


def merged_prs(
    subjects_by_repo: Mapping[str, Sequence[str]], overlay: ModelCloseoutScopeOverlay
) -> tuple[dict[str, list[str]], list[str]]:
    """Every listed repository's merged PRs by ticket, and the listed repositories whose
    subjects were not given (a clone that could not be read is named, never read as empty)."""
    merged: dict[str, list[str]] = {}
    unread: list[str] = []
    for repository in overlay.pr_repositories:
        subjects = subjects_by_repo.get(repository.name)
        if subjects is None:
            unread.append(repository.name)
            continue
        for ticket, prs in merged_from_subjects(
            repository.name, subjects, overlay
        ).items():
            merged.setdefault(ticket, []).extend(prs)
    return merged, unread


def open_prs(
    state: Mapping[str, Any], overlay: ModelCloseoutScopeOverlay
) -> dict[str, list[str]]:
    """Ticket id -> ``<repo>#<n>`` for every open PR whose title or head branch names it."""
    out: dict[str, list[str]] = {}
    for record in (state.get("prs") or {}).values():
        facts = record.get("facts") if isinstance(record, Mapping) else None
        if not isinstance(facts, Mapping) or facts.get("state") != "OPEN":
            continue
        repo = str(facts.get("repo") or "")
        if not repo or repo == overlay.evidence_repository:
            continue
        title = str(facts.get("title") or "")
        if title.startswith(overlay.evidence_subject_prefix):
            continue
        text = f"{title} {facts.get('head_ref') or ''}"
        for number in dict.fromkeys(_LOOSE_ID.findall(text)):
            out.setdefault(f"OMN-{int(number)}", []).append(
                f"{repo}#{facts.get('number')}"
            )
    return out


def pr_rows(
    tickets: Iterable[str],
    merged: Mapping[str, Sequence[str]],
    opened: Mapping[str, Sequence[str]] | None,
    error: str = "",
) -> list[dict[str, Any]]:
    """One row per ticket, input order, duplicates dropped. ``opened`` None means the open set
    was not read, and every row carries ``error``."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in tickets:
        ticket = raw.strip().upper()
        if not ticket or ticket in seen:
            continue
        seen.add(ticket)
        problem = error if opened is None else ""
        if not _TICKET.match(ticket):
            problem = "not a ticket id"
        rows.append(
            {
                "id": ticket,
                "merged": sorted(set(merged.get(ticket, ()))),
                "open": sorted(set((opened or {}).get(ticket, ()))),
                "error": problem,
            }
        )
    return rows
