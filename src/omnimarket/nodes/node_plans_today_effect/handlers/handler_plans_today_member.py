# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today's identity, member scope and member pull request source.

Every invocation resolves the caller through ``node_team_identity_compute``: the Linear subject the
sidecar records is offered as an authenticated Linear claim, the team identity declaration under
``ONEX_SKILL_OVERLAY_ROOTS`` is loaded, and the resolver decides. Only an authenticated operator match
runs the operator's mode. A resolved member runs member mode. Anything else (no sidecar, no declaration,
an unknown subject) is team restrictions and refuses with the resolver's diagnostic; it never runs the
operator's mode.

Member mode scopes the run to the tickets assigned to the member in Linear (the member's Linear user id
from the declaration) and the tickets named on the member's open CLAIMs (CLAIM rows whose ``principal=``
cell is one of the member's ledger principals, with no later TERMINAL from the same lane). A scoped
ticket's pull requests are the union of the ones Linear attaches to it and the ``pr=`` cells of the
member's ledger rows naming it; no GitHub search is made. Their state is read with one ``gh pr view``
per pull request under the member's own GitHub login, at most :data:`PR_READ_CAP` of them.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_team_identity_compute.handlers import (
    HandlerTeamIdentity,
    TeamIdentityError,
    load_team_identity,
)
from omnimarket.nodes.node_team_identity_compute.models import (
    ModelIdentityClaim,
    ModelTeamIdentityEntry,
    ModelTeamIdentityRequest,
    ModelTeamIdentityResult,
)

from ..models.model_plans_today import ModelLinearSidecar

PR_READ_CAP = 30
DEFAULT_OWNER = "OmniNode-ai"
UNREAD = "other member: state not read"
PR_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?$"
)
PR_CELL_RE = re.compile(r"^(?:([A-Za-z0-9_.-]+)/)?([A-Za-z0-9_.-]+)#(\d+)$")
RED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
)
GH_FIELDS = "state,isDraft,updatedAt,mergedAt,title,url,statusCheckRollup"


class MemberRefusedError(Exception):
    """Member mode cannot run; the message names the reason and, where there is one, the fix."""


Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


@dataclass(frozen=True)
class Identity:
    """The resolved caller: the mode, the member's entry (member mode only) and the resolver's result."""

    mode: str
    result: ModelTeamIdentityResult
    entry: ModelTeamIdentityEntry | None
    sidecar: ModelLinearSidecar | None
    sidecar_path: Path | None


@dataclass(frozen=True)
class PrRef:
    """One pull request: ``key`` is ``repo#n`` (the form plans and the ledger use)."""

    owner: str
    repo: str
    number: int

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


def sidecar_path_for(snapshot: Path | None) -> Path | None:
    """``linear-<date>.json`` beside the ``.txt`` snapshot; a ``.json`` snapshot is its own sidecar."""
    if snapshot is None:
        return None
    return snapshot if snapshot.suffix == ".json" else snapshot.with_suffix(".json")


def read_sidecar(path: Path | None) -> tuple[ModelLinearSidecar | None, str]:
    """(the sidecar, or None and why it could not be used)."""
    if path is None:
        return (
            None,
            "no Linear snapshot was given, so no sidecar names the signed-in subject",
        )
    if not path.is_file():
        return None, f"the Linear sidecar {path} is missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"the Linear sidecar {path} is unreadable ({type(exc).__name__})"
    try:
        return ModelLinearSidecar.model_validate(raw), ""
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        return None, f"the Linear sidecar {path} is malformed ({', '.join(fields)})"


def resolve_identity(
    snapshot: Path | None, overlay_roots: Sequence[str] | None
) -> Identity:
    """Resolve the caller on this invocation; refuse anything short of an operator or a member."""
    path = sidecar_path_for(snapshot)
    sidecar, why = read_sidecar(path)
    claims: tuple[ModelIdentityClaim, ...] = ()
    if sidecar is not None:
        claims = (ModelIdentityClaim(source="linear_auth", value=sidecar.subject),)
    result = HandlerTeamIdentity().handle(
        ModelTeamIdentityRequest(
            claims=claims,
            overlay_roots=None if overlay_roots is None else tuple(overlay_roots),
        )
    )
    if result.privileges == "operator":
        return Identity("operator", result, None, sidecar, path)
    if result.role != "member" or result.entry_id is None:
        reasons = [x for x in (why, result.diagnostic) if x] or [
            "the caller resolved to no member entry"
        ]
        raise MemberRefusedError(
            "team restrictions: "
            + "; ".join(reasons)
            + ". plans-today runs the operator's mode only for "
            "an authenticated operator and member mode only for a declared member"
        )
    try:
        resolved = load_team_identity(overlay_roots)
    except TeamIdentityError as exc:  # the declaration changed between the two reads
        raise MemberRefusedError(f"team restrictions: {exc}") from None
    entry = next((e for e in resolved.overlay.entries if e.id == result.entry_id), None)
    if entry is None or sidecar is None:
        raise MemberRefusedError(
            "team restrictions: the member entry vanished between reads"
        )
    if sidecar.subject != entry.linear_user_id:
        raise MemberRefusedError(
            f"the Linear sidecar {path} was read as another subject, not the member's declared Linear user"
        )
    return Identity("member", result, entry, sidecar, path)


def member_rows(rows: Iterable[Any], principals: Iterable[str]) -> list[Any]:
    """The ledger rows whose ``principal=`` cell is one of the member's ledger principals."""
    mine = frozenset(principals)
    return [r for r in rows if r.cell("principal") in mine]


def open_claim_tickets(rows: Sequence[Any]) -> set[str]:
    """Tickets named on the member's CLAIMs whose lane wrote no TERMINAL after the claim."""
    terminals: dict[str, list[Any]] = {}
    for r in rows:
        lane = r.cell("lane")
        if r.kind == "TERMINAL" and lane:
            terminals.setdefault(lane, []).append(r.ts)
    out: set[str] = set()
    for r in rows:
        if r.kind != "CLAIM":
            continue
        lane = r.cell("lane")
        if lane and any(t >= r.ts for t in terminals.get(lane, [])):
            continue
        out.update(r.names())
    return out


def parse_pr(text: str) -> PrRef | None:
    m = PR_URL_RE.match(text.strip())
    if m:
        return PrRef(m.group(1), m.group(2), int(m.group(3)))
    m = PR_CELL_RE.match(text.strip())
    if m:
        return PrRef(m.group(1) or DEFAULT_OWNER, m.group(2), int(m.group(3)))
    return None


@dataclass(frozen=True)
class MemberScope:
    """The member's tickets, their pull requests, and where each scoped ticket came from."""

    tickets: frozenset[str]
    assigned: frozenset[str]
    claimed: frozenset[str]
    prs_by_ticket: Mapping[str, tuple[PrRef, ...]]


def member_scope(
    entry: ModelTeamIdentityEntry, sidecar: ModelLinearSidecar, rows: Sequence[Any]
) -> MemberScope:
    """Scope the run to the member and discover each scoped ticket's pull requests (no GitHub search)."""
    mine = member_rows(rows, entry.ledger_principals)
    issues = {i.id: i for i in sidecar.issues}
    assigned = frozenset(
        i.id for i in sidecar.issues if i.assignee == entry.linear_user_id
    )
    claimed = frozenset(open_claim_tickets(mine))
    tickets = assigned | claimed
    prs: dict[str, tuple[PrRef, ...]] = {}
    for ticket in sorted(tickets):
        issue = issues.get(ticket)
        if issue is None or issue.pull_requests is None:
            raise MemberRefusedError(
                f"{ticket} is in the member's scope but its Linear attachments could not be read "
                "(the sidecar holds no attachment read for it); rerun the snapshot's step 1"
            )
        found: dict[str, PrRef] = {}
        for url in issue.pull_requests:
            ref = parse_pr(url)
            if ref is None:
                raise MemberRefusedError(
                    f"{ticket}: Linear attachment {url!r} is not a GitHub pull request URL"
                )
            found.setdefault(ref.key, ref)
        for r in mine:
            if ticket in r.names():
                for cell in (c for c in r.cell("pr").split(",") if c):
                    ref = parse_pr(cell)
                    if ref is not None:
                        found.setdefault(ref.key, ref)
        prs[ticket] = tuple(found[k] for k in sorted(found))
    return MemberScope(tickets, assigned, claimed, prs)


def _cls(rollup: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    red: list[str] = []
    pending = False
    for check in rollup:
        name = str(check.get("name") or check.get("context") or "check")
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in RED_CONCLUSIONS or state in RED_CONCLUSIONS:
            red.append(name)
        elif (status and status != "COMPLETED") or state in ("PENDING", "EXPECTED"):
            pending = True
    if red:
        return "red", red
    return ("pending" if pending else "green"), red


def _ts(value: Any) -> str | None:
    if not value:
        return None
    return str(value).replace(".000Z", "Z")


def read_member_prs(
    scope: MemberScope, github_login: str, run: Runner, timeout: float
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], int]:
    """(watcher-shaped PR entries, keys by ticket, reads made). Refuses past the cap or on a failed read."""
    refs: dict[str, PrRef] = {}
    for ticket_refs in scope.prs_by_ticket.values():
        for ref in ticket_refs:
            refs.setdefault(ref.key, ref)
    if len(refs) > PR_READ_CAP:
        raise MemberRefusedError(
            f"the member's scope holds {len(refs)} pull requests, more than the {PR_READ_CAP} reads allowed; "
            "narrow it with --plan or close the claims that no longer hold"
        )
    prs: dict[str, dict[str, Any]] = {}
    if refs:
        login = _gh(
            run,
            ["gh", "api", "user", "--jq", ".login"],
            timeout,
            "the signed-in GitHub login",
        ).strip()
        if login != github_login:
            raise MemberRefusedError(
                "gh is signed in as another GitHub login, not the member's declared one; the member's "
                "pull requests are read only under the member's own login"
            )
    for key in sorted(refs):
        ref = refs[key]
        out = _gh(
            run,
            [
                "gh",
                "pr",
                "view",
                str(ref.number),
                "--repo",
                f"{ref.owner}/{ref.repo}",
                "--json",
                GH_FIELDS,
            ],
            timeout,
            f"pull request {key}",
        )
        try:
            data = json.loads(out)
            state = str(data["state"]).upper()
            rollup = data.get("statusCheckRollup") or []
        except (ValueError, KeyError, TypeError, AttributeError):
            raise MemberRefusedError(
                f"the read of pull request {key} returned no usable state"
            ) from None
        cls, red = _cls(rollup)
        prs[key] = {
            "cls": cls,
            "ci": {"red": red},
            "facts": {
                "state": state,
                "title": str(data.get("title") or ""),
                "updated_at": _ts(data.get("updatedAt")),
                "merged_at": _ts(data.get("mergedAt")),
                "draft": bool(data.get("isDraft")),
            },
        }
    by_ticket: dict[str, list[str]] = {}
    for ticket, ticket_refs in scope.prs_by_ticket.items():
        keys = [
            r.key
            for r in ticket_refs
            if prs[r.key]["facts"]["state"] in ("OPEN", "MERGED")
        ]
        if keys:
            by_ticket[ticket] = keys
    return prs, by_ticket, len(refs)


def _gh(run: Runner, argv: list[str], timeout: float, what: str) -> str:
    try:
        r = run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MemberRefusedError(
            f"the read of {what} timed out after {timeout:g} s"
        ) from None
    except OSError as exc:
        raise MemberRefusedError(
            f"the read of {what} failed ({type(exc).__name__})"
        ) from None
    if r.returncode != 0:
        detail = str(r.stderr or "").strip().splitlines()
        raise MemberRefusedError(
            f"the read of {what} failed (exit {r.returncode}): {(detail[-1] if detail else 'no output')[:200]}"
        )
    return str(r.stdout)
