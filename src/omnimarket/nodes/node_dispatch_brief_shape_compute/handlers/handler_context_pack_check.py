# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief checker's refusals for a code brief's context pack.

A code brief carries a context pack the orchestrator approved. A code brief is every brief except one that states
``Work kind: read-only`` or ``Work kind: document``, or is open-ended (a ``Lane kind: open-ended`` line, or a lane name
holding one of the open-ended words). Any ``Work kind: code`` line wins over every other kind line, and a ``Work kind:``
value that is none of the three is refused. A brief that states no kind at all is therefore a code brief.

The block, in the brief's own prose (outside the rules block and the 3a block)::

    CONTEXT PACK
    final_pack_hash=<64 hex, optional sha256: prefix>
    pack-approved-by=orchestrator <YYYY-MM-DDTHH:MM:SSZ>

    ```pack
    <the pack body: the `## <tier>` sections the context pack orchestrator built, as written>
    ```

The header is the lines directly under the heading up to the first blank line. The body sits in the fenced block (info
string ``pack``) after the header; a longer fence holds a body that carries fences. ``final_pack_hash`` is the sha256 of the
body's UTF-8 text, the lines between the fences joined by newlines, no trailing newline. The approval stamp is the
timestamp of a ledger row written by an orchestrator lane (``lane=orchestrator`` or ``lane=orchestrator-<suffix>``, or
``from=`` on a MSG) that names the same hash.
"""

from __future__ import annotations

import hashlib
import re

from ..models.model_dispatch_brief_shape import (
    ModelContextPackCheckRequest,
    ModelContextPackCheckResult,
)

PACK_HEADING_RE = re.compile(r"^CONTEXT PACK[ \t]*$", re.MULTILINE)
PACK_HASH_RE = re.compile(
    r"^final_pack_hash=(?:sha256:)?([0-9a-f]{64})[ \t]*$", re.MULTILINE
)
PACK_APPROVAL_RE = re.compile(
    r"^pack-approved-by=orchestrator "
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)[ \t]*$",
    re.MULTILINE,
)
PACK_OPEN_RE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*pack[ \t]*")
WORK_KIND_LINE_RE = re.compile(
    r"^[ \t]*Work kind:[ \t]*(.*?)[ \t]*$", re.MULTILINE | re.IGNORECASE
)
OPEN_ENDED_LINE_RE = re.compile(
    r"^[ \t]*Lane kind:[ \t]*open-ended[ \t]*$", re.MULTILINE | re.IGNORECASE
)
LANE_CLAIM_CELL_RE = re.compile(r'\$\(date -u[^)]*\) \| CLAIM \| lane=([^\s|"]+)')
LANE_LINE_RE = re.compile(r"^[ \t]*Lane:[ \t]*([^\s|]+)", re.MULTILINE)
WORK_KINDS = ("code", "read-only", "document")
PACK_SOURCE = (
    "the pack is built by node_context_pack_orchestrator under the "
    "lane_brief_context profile"
)
NO_PACK_BLOCK = (
    "a code brief carries no CONTEXT PACK block (a column-zero `CONTEXT PACK` line, "
    "the final_pack_hash and pack-approved-by lines under it, then the pack body in a "
    f"fenced `pack` block); {PACK_SOURCE}. A brief with no code work states "
    "`Work kind: read-only` or `Work kind: document`"
)
NO_PACK_HASH = (
    "the CONTEXT PACK block carries no final_pack_hash (`final_pack_hash=<64 hex>` on a "
    "line directly under the `CONTEXT PACK` heading: a blank line ends the header, so "
    "no blank line sits between the heading and the lines and nothing of the header "
    "follows the body)"
)
NO_PACK_APPROVAL = (
    "the CONTEXT PACK block carries no pack-approved-by=orchestrator <ledger row stamp> "
    "line (`pack-approved-by=orchestrator <YYYY-MM-DDTHH:MM:SSZ>` directly under the "
    "`CONTEXT PACK` heading, before the first blank line)"
)
NO_PACK_BODY = (
    "the CONTEXT PACK block carries no pack body (a fenced block opened with ```pack "
    f"right after the header, closed by a fence as long as the opener); {PACK_SOURCE}"
)
EMPTY_PACK_BODY = f"the CONTEXT PACK body is empty: the fenced pack block holds no text; {PACK_SOURCE}"


def work_kind_problems(prose: str) -> list[str]:
    return [
        f"unknown Work kind {value!r}: a brief states `Work kind: code`, "
        "`Work kind: read-only` or `Work kind: document`"
        for value in WORK_KIND_LINE_RE.findall(prose)
        if value.lower() not in WORK_KINDS
    ]


def is_code_brief(prose: str, brief: str, open_ended_words: tuple[str, ...]) -> bool:
    """A code brief: any `Work kind: code` line, else any brief that is not read-only, document or open-ended.

    Open-ended is a kind line or a lane name (from the brief's ``Lane:`` line or CLAIM cell) holding an open-ended word.
    With no kind line at all it is a code brief.
    """
    kinds = {value.lower() for value in WORK_KIND_LINE_RE.findall(prose)}
    if "code" in kinds:
        return True
    source = brief or prose
    lanes = LANE_LINE_RE.findall(source) + LANE_CLAIM_CELL_RE.findall(source)
    return not (
        kinds & {"read-only", "document"}
        or OPEN_ENDED_LINE_RE.search(prose)
        or any(
            word in re.split(r"[-_.:]", lane.lower())
            for lane in lanes
            for word in open_ended_words
        )
    )


def pack_body(brief: str, block: str) -> str | None:
    """The fenced `pack` body under the block's header in the raw brief; None when absent."""
    at = brief.find(block)
    if at < 0:
        return None
    lines = brief[at + len(block) :].split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    opening = PACK_OPEN_RE.fullmatch(lines[i]) if i < len(lines) else None
    if opening is None:
        return None
    fence = opening[1]
    close = re.compile(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*")
    for j in range(i + 1, len(lines)):
        if close.fullmatch(lines[j]):
            return "\n".join(lines[i + 1 : j])
    return None


def approval_problem(
    stamp: str, digest: str, rows: tuple[str, ...] | None, error: str
) -> str | None:
    """Why the ledger does not confirm the approval stamp: no row at that stamp from an orchestrator lane naming the hash."""
    if error:
        return f"pack-approved-by cannot be confirmed: the ledger could not be read ({error})"
    if rows is None:
        return "pack-approved-by cannot be confirmed: no ledger reader was given"
    from_orchestrator = []
    for row in rows:
        if stamp not in row:
            continue
        parts = [p.strip() for p in row.split(" | ")]
        cells = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
        owner = cells.get("lane", cells.get("from", "")).split(" ")[0]
        if parts[0] == stamp and re.match(r"orchestrator(?:-|$)", owner):
            if digest in row.lower():
                return None
            from_orchestrator.append(row)
    if from_orchestrator:
        return (
            f"pack-approved-by names ledger row {stamp}, written by the orchestrator, "
            f"but that row does not name final_pack_hash {digest}: the approval is for "
            "another pack"
        )
    return (
        f"pack-approved-by names ledger row {stamp}, but the ledger holds no row at "
        "that stamp written by an orchestrator lane; the orchestrator records its "
        f"approval of final_pack_hash {digest} first"
    )


class HandlerContextPackCheck:
    """The refusals of a code brief's context pack, one per defect."""

    def handle(
        self, request: ModelContextPackCheckRequest
    ) -> ModelContextPackCheckResult:
        prose = request.prose
        problems = work_kind_problems(prose)
        if not is_code_brief(prose, request.brief, request.open_ended_words):
            return ModelContextPackCheckResult(problems=tuple(problems))
        heading = PACK_HEADING_RE.search(prose)
        if heading is None:
            return ModelContextPackCheckResult(problems=(*problems, NO_PACK_BLOCK))
        block = prose[heading.start() :].split("\n\n", 1)[0]
        declared = PACK_HASH_RE.search(block)
        approval = PACK_APPROVAL_RE.search(block)
        problems += [
            why
            for found, why in ((declared, NO_PACK_HASH), (approval, NO_PACK_APPROVAL))
            if not found
        ]
        body = pack_body(request.brief, block)
        if body is None:
            problems.append(NO_PACK_BODY)
        elif not body.strip():
            problems.append(EMPTY_PACK_BODY)
        elif declared:
            actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if actual != declared.group(1):
                problems.append(
                    f"the CONTEXT PACK body hashes to sha256 {actual} but the block's "
                    f"final_pack_hash is {declared.group(1)}: the body was edited after "
                    "the pack was built, or the hash is not its own"
                )
        if approval and declared:
            why = approval_problem(
                approval.group(1),
                declared.group(1),
                request.ledger_rows,
                request.ledger_error,
            )
            if why:
                problems.append(why)
        return ModelContextPackCheckResult(problems=tuple(problems))
