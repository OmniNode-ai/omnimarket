# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence handles, their binding and the verdict of the reconciler (OMN-17466, moved by OMN-20677)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .ledger_rows import (
    PR_FIELD_RE,
    PR_REF_RE,
    SHA_RE,
    TICKET_RE,
    Row,
)

MAX_SHA_HANDLES = 12


@dataclass
class PrHandle:
    raw_repo: str
    repo: str | None  # resolved canonical repo name, None if unresolvable
    number: int
    state: str = "UNVERIFIED"  # MERGED | OPEN | CLOSED | LOOKUP_FAILED | UNVERIFIED
    merged_at: str = ""
    merge_sha: str = ""
    title: str = ""
    # Binding (see bind_evidence): a handle that provably belongs to OTHER
    # work — its title cites only different tickets, or it landed before the
    # claim existed — is excluded from the verdict instead of proving it.
    bound: bool = True
    exclude_reason: str = ""


@dataclass
class ShaHandle:
    sha: str
    found_in: str = ""
    landed: bool = False
    committer_ts: datetime | None = None
    msg_tickets: frozenset[str] = field(default_factory=frozenset)
    bound: bool = True
    exclude_reason: str = ""


@dataclass
class Evidence:
    prs: list[PrHandle] = field(default_factory=list)
    shas: list[ShaHandle] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceContext:
    """What a deployment contributes to reading a row's evidence handles."""

    clones: tuple[str, ...]
    aliases: Mapping[str, str]
    branch_prefixes: tuple[str, ...]
    registry_name: str


def resolve_repo(
    raw: str, clones: Collection[str], aliases: Mapping[str, str]
) -> str | None:
    lowered = raw.lower()
    if lowered in aliases:
        return aliases[lowered]
    if raw in clones:
        return raw
    return None


def extract_evidence(
    body: str,
    clones: Collection[str],
    aliases: Mapping[str, str],
    branch_prefixes: Collection[str] = (),
) -> Evidence:
    evidence = Evidence()
    seen_prs: set[tuple[str, int]] = set()
    refs = PR_REF_RE.findall(body)
    repo_field = re.search(r"(?:^|[\s|])repo=([^\s|;]+)", body)
    if repo_field is not None:
        for value in PR_FIELD_RE.findall(body):
            refs.extend(
                (repo_field.group(1), token)
                for token in value.split(",")
                if token.isdigit()
            )
    for raw_repo, num in refs:
        repo = resolve_repo(raw_repo, clones, aliases)
        key = (repo or raw_repo, int(num))
        if key in seen_prs:
            continue
        seen_prs.add(key)
        evidence.prs.append(PrHandle(raw_repo=raw_repo, repo=repo, number=int(num)))
    seen_shas: set[str] = set()
    for sha in SHA_RE.findall(body):
        if sha in seen_shas or len(seen_shas) >= MAX_SHA_HANDLES:
            continue
        seen_shas.add(sha)
        evidence.shas.append(ShaHandle(sha=sha))
    if branch_prefixes:
        branch_re = re.compile(
            r"\b(?:"
            + "|".join(re.escape(p) for p in branch_prefixes)
            + r")/[A-Za-z0-9._/-]{3,}"
        )
        evidence.branches = sorted(set(branch_re.findall(body)))
    return evidence


def candidate_clone_names(
    body: str, evidence: Evidence, clones: Collection[str], registry_name: str
) -> tuple[str, ...]:
    """The registry root first, then the clones of the cited PRs' repos, then any
    clone the row's text names: the order a commit is looked for in."""
    ordered: list[str] = [registry_name]
    for pr in evidence.prs:
        if pr.repo and pr.repo in clones and pr.repo not in ordered:
            ordered.append(pr.repo)
    for name in clones:
        if name in body and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


COMPLETED = "COMPLETED"
ORPHANED = "ORPHANED"
UNKNOWN = "UNKNOWN"


def parse_iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def bind_evidence(claim: Row, evidence: Evidence) -> None:
    """Exclude handles that provably belong to OTHER work, so a fence-check
    citation of a peer lane's PR can never auto-close this claim.

    Rules, in order. (1) A MERGED PR / commit made BEFORE the claim is never
    its deliverable, whatever tickets it shares: a claim cites its
    preconditions and dependencies with their merge times, and OMN-17573
    measured 7 of 25 auto-closes citing exactly those. (2) When both the claim
    and the handle carry ticket ids, they must intersect (PR titles carry
    an OMN ticket id by CI mandate; commit messages likewise). OPEN/CLOSED PRs with
    indeterminate binding stay bound: the worst outcome there is a
    NEEDS-ATTENTION row, never a fabricated close."""
    for pr in evidence.prs:
        if pr.repo is None:
            continue
        merged = parse_iso(pr.merged_at) if pr.state == "MERGED" else None
        if merged is not None and merged < claim.ts:
            pr.bound = False
            pr.exclude_reason = "merged before the claim was made"
            continue
        pr_tickets = frozenset(TICKET_RE.findall(pr.title))
        if claim.tickets and pr_tickets and not (claim.tickets & pr_tickets):
            pr.bound = False
            pr.exclude_reason = f"title cites {'/'.join(sorted(pr_tickets))}, not this claim's ticket(s)"
    for sha in evidence.shas:
        if not sha.found_in:
            continue
        if sha.committer_ts is not None and sha.committer_ts < claim.ts:
            sha.bound = False
            sha.exclude_reason = "commit predates the claim"
            continue
        if claim.tickets and sha.msg_tickets and not (claim.tickets & sha.msg_tickets):
            sha.bound = False
            sha.exclude_reason = (
                f"commit message cites {'/'.join(sorted(sha.msg_tickets))}, "
                f"not this claim's ticket(s)"
            )


def _excluded_note(evidence: Evidence) -> str:
    excluded = [
        f"{p.repo}#{p.number} ({p.exclude_reason})" for p in evidence.prs if not p.bound
    ] + [f"{s.sha[:12]} ({s.exclude_reason})" for s in evidence.shas if not s.bound]
    return (
        "; excluded as other lanes' evidence: " + ", ".join(excluded)
        if excluded
        else ""
    )


def verdict_for(evidence: Evidence) -> tuple[str, str]:
    prs = evidence.prs
    resolvable = [p for p in prs if p.repo is not None and p.bound]
    unbound_prs = [p for p in prs if p.repo is not None and not p.bound]
    if any(p.state in ("OPEN", "CLOSED") for p in resolvable):
        dead = [
            f"{p.repo}#{p.number} {p.state}"
            for p in resolvable
            if p.state in ("OPEN", "CLOSED")
        ]
        return ORPHANED, "not landed: " + ", ".join(dead) + _excluded_note(evidence)
    if any(p.repo is None for p in prs):
        return UNKNOWN, "a cited PR has an unrecognized repo alias"
    if resolvable:
        if all(p.state == "MERGED" for p in resolvable):
            detail = ", ".join(
                f"{p.repo}#{p.number} MERGED {p.merged_at} merge {p.merge_sha[:12]}"
                for p in resolvable
            )
            return COMPLETED, detail + _excluded_note(evidence)
        failed = [
            f"{p.repo}#{p.number} {p.state}" for p in resolvable if p.state != "MERGED"
        ]
        return UNKNOWN, "unresolvable PR handle(s): " + ", ".join(failed)
    if unbound_prs:
        return (
            UNKNOWN,
            "every cited PR handle belongs to other work" + _excluded_note(evidence),
        )
    if prs:
        raw = ", ".join(f"{p.raw_repo}#{p.number}" for p in prs)
        return UNKNOWN, f"PR handle(s) with unrecognized repo alias: {raw}"
    if evidence.shas:
        bound = [s for s in evidence.shas if s.bound]
        found = [s for s in bound if s.found_in]
        missing = [s.sha[:12] for s in bound if not s.found_in]
        if not bound:
            return (
                UNKNOWN,
                "every cited SHA belongs to other work" + _excluded_note(evidence),
            )
        if not found:
            return UNKNOWN, "no cited SHA resolves in any canonical clone"
        if missing:
            return UNKNOWN, "unresolved SHA(s): " + ", ".join(missing)
        unlanded = [s.sha[:12] for s in found if not s.landed]
        if unlanded:
            return UNKNOWN, "SHA(s) present but not on a landing ref: " + ", ".join(
                unlanded
            )
        detail = ", ".join(f"{s.sha[:12]} landed in {s.found_in}" for s in found)
        return COMPLETED, detail + _excluded_note(evidence)
    return UNKNOWN, "no extractable evidence handles"
