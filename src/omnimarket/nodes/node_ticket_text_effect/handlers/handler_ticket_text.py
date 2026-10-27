# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a ticket's own words from Linear and put them in a lane brief (OMN-17427).

A headless lab lane has no Linear tool, so a brief that names its ticket by id alone leaves the lane unable to read
what it is asked to meet: on 2026-10-17, of 269 backlog lanes that ended, 46 ended blocked and several named exactly
that. The ticket's title, state and description travel in the brief instead, read once where the key is: the
remote-lane runner appends the block at dispatch, ``lane_brief.py ticket-text`` prints it, and ``check --lab`` refuses
a lab-bound brief without it, and refuses one whose ticket has no description (OMN-20607). Every line taken from the ticket is quoted (``> ``),
so its words are never read as the brief's premises, mint asks or instructions. The key is the caller's; it is sent
only to Linear or a loopback endpoint and is never printed or returned.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, cast

from ..models.model_ticket_text import ModelTicketTextRequest, ModelTicketTextResult

LINEAR_QUERY = (
    "query($id: String!) { issue(id: $id) { identifier title description "
    "state { name type } children(first: 1) { nodes { id } } "
    "attachments(first: 100) { nodes { sourceType title metadata } } } }"
)
TICKET_ID_RE = re.compile(r"\bOMN-[0-9]+\b")
TICKET_TEXT_MAX = 16000
TICKET_TEXT_MARKER_RE = re.compile(
    r"^[ \t]*ticket-text:[ \t]*(OMN-[0-9]+)\b[^\n]*$", re.MULTILINE
)
QUOTE_MARKER_RE = re.compile(
    r"^[ \t]*ticket-criteria:[ \t]*(OMN-[0-9]+)[ \t]*$", re.MULTILINE
)
CRITERION_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.*)$", re.MULTILINE)
CRITERION_LINE_RE = re.compile(r"^[ \t]*[-*][ \t]*\[[ xX]\][ \t]*\S.*$", re.MULTILINE)


def ticket_id(value: str) -> str:
    """argparse type for an ``OMN-<n>`` id."""
    if not TICKET_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an OMN-<n> id")
    return value


class TicketTextError(Exception):
    """Linear did not answer the read, or the key may not be sent to the endpoint."""


def linear_url_ok(url: str) -> bool:
    """True for Linear itself or a loopback endpoint: the only places the key is sent."""
    parts = urllib.parse.urlsplit(url)
    try:
        loopback = (
            parts.hostname == "localhost"
            or ipaddress.ip_address(parts.hostname or "").is_loopback
        )
    except ValueError:
        loopback = False
    return (parts.scheme == "https" and parts.hostname == "api.linear.app") or (
        parts.scheme in ("http", "https") and loopback
    )


def read_ticket(ident: str, url: str, key: str) -> dict[str, Any] | None:
    """One live Linear read; None when Linear has no such ticket. Never prints the key."""
    if not linear_url_ok(url):
        raise TicketTextError(
            f"{url!r} is neither Linear nor a loopback endpoint; the key is not sent"
        )
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": LINEAR_QUERY, "variables": {"id": ident}}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            body = json.load(reply)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {}
        if any(
            "not found" in str(e.get("message", "")).lower()
            for e in body.get("errors") or []
        ):
            return None
        raise TicketTextError(
            f"Linear refused the read of {ident}: HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TicketTextError(f"Linear unreachable reading {ident}: {exc}") from exc
    if body.get("errors") or not (body.get("data") or {}).get("issue"):
        raise TicketTextError(
            f"Linear returned no issue for {ident}: {body.get('errors')}"
        )
    return cast("dict[str, Any]", body["data"]["issue"])


def norm_criterion(line: str) -> str:
    """One criterion line in the form both sides of the quote check compare.

    The live side is the text after the checkbox (``CRITERION_RE``) and the brief side is the whole quoted line, so
    this strips the quote marker, a list bullet (only one followed by a space or the checkbox, so the opening ``**`` of
    a bold criterion is never taken for a bullet), the checkbox, link targets and the emphasis marks, the same way
    whichever side the line came from."""
    line = re.sub(r"^>\s?", "", line.strip()).strip()
    line = re.sub(r"^(?:[-*+](?=[ \t\[]))?\s*(?:\[[ xX]\])?\s*", "", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"\*+|(?<![A-Za-z0-9])_+(?=\S)|(?<=\S)_+(?![A-Za-z0-9])", "", line)
    return " ".join(line.split())


def quoted_lines(text: str, end: int) -> list[str]:
    """The ``>`` lines directly under the marker line that ends at ``end``."""
    out: list[str] = []
    for line in text[end:].splitlines()[1:]:
        if not line.lstrip().startswith(">"):
            break
        out.append(line)
    return out


def unfenced_prose(text: str) -> str:
    """Exclude Markdown code examples from the standing-rule check (OMN-18397)."""
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        if fence:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*",
                line.rstrip("\r\n"),
            ):
                fence = ""
            lines.append("\n")
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = opening[1]
            lines.append("\n")
        else:
            lines.append(line)
    return "".join(lines)


def without_ticket_text(text: str) -> str:
    """``text`` without its ``ticket-text:`` marker lines and the quoted lines under each."""
    out: list[str] = []
    quoting = False
    for line in text.splitlines(keepends=True):
        if TICKET_TEXT_MARKER_RE.fullmatch(line.rstrip("\r\n")):
            quoting = True
            continue
        if quoting and line.lstrip().startswith(">"):
            continue
        quoting = False
        out.append(line)
    return "".join(out)


def has_ticket_text(brief: str, ident: str) -> bool:
    """True when the brief's prose carries a ``ticket-text: <ident>`` marker with quoted lines under it."""
    prose = unfenced_prose(brief)
    return any(
        m.group(1) == ident and quoted_lines(prose, m.end())
        for m in TICKET_TEXT_MARKER_RE.finditer(prose)
    )


def ticket_text_block(issue: dict[str, Any], read_at: str) -> str:
    """The brief section carrying one ticket's own words, as read from Linear at ``read_at``."""
    ident = issue["identifier"]
    desc = (issue.get("description") or "").strip()
    criteria = [m.group(0).strip() for m in CRITERION_LINE_RE.finditer(desc)]
    body = desc[:TICKET_TEXT_MAX]
    if len(desc) > TICKET_TEXT_MAX:
        body += f"\n[{len(desc) - TICKET_TEXT_MAX} more characters are on the ticket]"
    lines = [
        f"## Ticket {ident}, read from Linear at {read_at}",
        "",
        (
            "The ticket's own title, state, description and acceptance criteria, read once with the launching "
            "host's key so a lane with no Linear tool reads them here. Quoted as they stand; never "
            "edit a criterion's own line on the ticket."
        ),
        "",
    ]
    if criteria:
        lines += [f"ticket-criteria: {ident}", *(f"> {c}" for c in criteria), ""]
    lines += [
        f"ticket-text: {ident} read={read_at}",
        f"> Title: {' '.join((issue.get('title') or '').split())}",
        f"> State: {(issue.get('state') or {}).get('name') or 'unknown'}",
        ">",
        *(
            (f"> {x}".rstrip() if x.strip() else ">")
            for x in (body or "(the ticket has no description)").splitlines()
        ),
    ]
    return "\n".join(lines) + "\n"


def empty_reason(ident: str) -> str:
    """The named refusal for a ticket with no description: nothing to embed, and a lab lane has no tool to read more."""
    return (
        f"ticket-text-empty: {ident} has no description, so there is no acceptance criterion to embed in a "
        "lab-bound brief and a headless lab lane has no Linear key or tool to read one (OMN-20607); write the "
        "ticket's description and criteria first"
    )


def absent_reason(issue: dict[str, Any] | None, ident: str) -> str:
    """Why there is no ticket text to embed: ``ident`` is not in Linear, or has no description. Empty when there is."""
    if issue is None:
        return f"{ident} is not found in Linear"
    return "" if (issue.get("description") or "").strip() else empty_reason(ident)


def text_problems(brief: str, premise: str, ident: str, desc: str) -> list[str]:
    """Why a lab-bound brief does not carry its ticket's words; ``ticket-text-empty`` when the ticket has no
    description, since a lab lane cannot read the ticket itself (OMN-20607).

    ``premise`` is the brief's own prose (the ticket-text blocks left out), read for the ``ticket-criteria`` quote."""
    if not desc.strip():
        return [empty_reason(ident)]
    fix = f"run `lane_brief.py ticket-text --ticket {ident}` and paste its block into the brief"
    if not has_ticket_text(brief, ident):
        return [
            f"{ident} has a Linear description and this lab-bound brief carries none of its text (no "
            f"`ticket-text: {ident}` block): a headless lab lane has no Linear tool to read its acceptance "
            f"criteria (OMN-17427); {fix}"
        ]
    quoted = {
        norm_criterion(x)
        for m in QUOTE_MARKER_RE.finditer(premise)
        if m.group(1) == ident
        for x in quoted_lines(premise, m.end())
    }
    live = [norm_criterion(c) for c in CRITERION_RE.findall(desc)]
    left = [c for c in live if c and c not in quoted]
    if not left:
        return []
    return [
        f"the brief's `ticket-criteria: {ident}` quote omits {len(left)} of the ticket's {len(live)} "
        f'criterion lines (first: "{left[0][:120]}"); {fix}'
    ]


class HandlerTicketText:
    """The dispatch-time read: append the ticket's block to the brief, or refuse the dispatch with a named reason.

    A headless lab lane has no Linear key or tool (nine lanes ended ticket-unreadable between 2026-10-17 and
    2026-10-19, OMN-20607), so a ticket the launching host cannot read (``ticket-unreadable``) or that has no
    description (``ticket-text-empty``) is a refusal here, before placement, never a note telling the lane to read it."""

    def handle(self, request: ModelTicketTextRequest) -> ModelTicketTextResult:
        result = self.read(request)
        if request.echo is not None:
            for line in result.echoes(request.lane):
                request.echo(line)
        return result

    def read(self, request: ModelTicketTextRequest) -> ModelTicketTextResult:
        brief, ticket = request.brief, request.ticket
        if request.verbatim:
            return ModelTicketTextResult(brief, ticket, "verbatim")
        if not ticket:
            return ModelTicketTextResult(brief, None, "no-ticket")
        if has_ticket_text(brief, ticket):
            return ModelTicketTextResult(brief, ticket, "brief")
        try:
            url, key = request.endpoint()
            issue = read_ticket(ticket, url, key)
            if issue is None:
                raise TicketTextError(f"{ticket} is not found in Linear")
        except TicketTextError as exc:
            return ModelTicketTextResult(
                brief,
                ticket,
                "refused",
                error=str(exc)[:300],
                refusal="ticket-unreadable",
            )
        if reason := absent_reason(issue, ticket):
            return ModelTicketTextResult(
                brief, ticket, "refused", error=reason, refusal="ticket-text-empty"
            )
        read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = ticket_text_block(issue, read_at)
        return ModelTicketTextResult(
            brief.rstrip("\n") + "\n\n" + block,
            ticket,
            "linear",
            read_at=read_at,
            criteria=len(CRITERION_LINE_RE.findall(issue.get("description") or "")),
            chars=len(block),
        )
