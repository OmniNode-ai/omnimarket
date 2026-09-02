# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure composer for the contractor integration note (OMN-17277).

Zero I/O. Every input is passed in; every output is a function of those inputs.
This module is where the whole decision lives, so the decision is unit-testable
without a Linear key, a git checkout, or a network.

Three rules shape the output, and each one exists because of a specific failure:

1. **The machine never invents a probe.** ``Probe to run`` and ``Pass
   expectation`` are judgement, not facts derivable from a diff. When the
   merging lane did not supply them, the note says so in the field rather than
   guessing — a fabricated probe is worse than an absent one, because the
   validator runs it and files the wrong finding.

2. **Internal references are withheld whole, and named.** A field whose source
   text carries an operator path, a worktree path, a lane name, or a session id
   is dropped entirely and listed in ``redacted_fields``. Partial redaction of a
   paragraph leaves a misleading half-sentence; a silently emptied field reads
   as "nothing to say".

3. **Reachability is stated, never assumed.** A change on ``dev`` with no
   containing tag is not available to someone installing from a release, so the
   note carries the exact pin recipe instead of implying the change is live.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-17274: Lakshman customer-plane validation charter (epic)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelContractorRoster,
    ModelContractorRosterEntry,
    ModelMergedPullRequest,
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    EnumNoteSkipReason,
    EnumReachability,
    ModelIntegrationNoteDecision,
)

# --------------------------------------------------------------------------
# Ticket reference
# --------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"\bOMN-(\d+)\b", re.IGNORECASE)

# --------------------------------------------------------------------------
# Internal-reference refusal (rule 2)
#
# Every pattern here is something the recipient cannot act on and must not see:
# operator-machine paths, the canonical registry and worktree roots, ledger lane
# tags, session/correlation handles, and RFC1918 lab addresses. The list is
# deliberately conservative — a false positive costs one named field, a false
# negative leaks an internal surface into a contractor's inbox.
# --------------------------------------------------------------------------

_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/Users/"),
    re.compile(r"/Volumes/"),
    re.compile(r"\$?OMNI_HOME", re.IGNORECASE),
    re.compile(r"\bomni_home\b", re.IGNORECASE),
    re.compile(r"\bomni_worktrees\b", re.IGNORECASE),
    re.compile(r"\.onex_state\b"),
    re.compile(r"\bROLLING_WORK_LEDGER\b", re.IGNORECASE),
    re.compile(r"\blane=", re.IGNORECASE),
    re.compile(r"\bsession[_-]?id\b", re.IGNORECASE),
    re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
)


def contains_internal_reference(text: str) -> bool:
    """True when ``text`` names a surface the recipient cannot open or act on."""
    return any(pattern.search(text) for pattern in _INTERNAL_PATTERNS)


def extract_ticket_reference(pull_request: ModelMergedPullRequest) -> str | None:
    """Return the ticket key cited by the PR title, else the body, else None.

    Title first on purpose: the title is the surface the repo's own pr-title
    gate already enforces, so it is the citation the author committed to. A body
    reference is a fallback, not a peer — a body can cite three tickets in
    passing while the title names the one the work is for.
    """
    for source in (pull_request.title, pull_request.body):
        match = _TICKET_PATTERN.search(source or "")
        if match is not None:
            return f"OMN-{match.group(1)}"
    return None


def match_contractor(
    assignee_linear_user_id: str | None,
    roster: ModelContractorRoster,
) -> ModelContractorRosterEntry | None:
    """Return the roster entry for this assignee, or None.

    Matching is on the Linear user UUID only. Display names are user-editable
    and collide; the UUID is what Linear itself joins on.
    """
    if not assignee_linear_user_id:
        return None
    for entry in roster.contractors:
        if entry.linear_user_id == assignee_linear_user_id:
            return entry
    return None


# --------------------------------------------------------------------------
# Author-supplied fields
# --------------------------------------------------------------------------

_AUTHOR_BLOCK_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*integration\s+note\b.*$", re.IGNORECASE
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_LABELS: dict[str, str] = {
    "what changed": "what_changed",
    "what it means for your surfaces": "surfaces",
    "what it means": "surfaces",
    "probe to run": "probe",
    "pass expectation": "pass_expectation",
}
_LABEL_LINE = re.compile(
    r"^\s{0,3}[-*]?\s*\**\s*(?P<label>[A-Za-z][A-Za-z ']{2,40}?)\s*\**\s*:\s*(?P<value>.*)$"
)


def extract_authored_fields(body: str) -> dict[str, str]:
    """Parse the optional ``Integration note`` block out of a PR body.

    The block is a markdown heading named "Integration note" followed by
    ``Label: value`` lines. A value continues across following lines until the
    next recognised label, the next heading, or the end of the body — so a
    multi-line probe survives intact.

    An absent block is not an error. It is the normal case for a merge that
    touches nothing the validator probes, and the composer says so per field.
    """
    lines = (body or "").splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if _AUTHOR_BLOCK_HEADING.match(line):
            start = index + 1
            break
    if start is None:
        return {}

    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start:]:
        if _HEADING.match(line) and not _AUTHOR_BLOCK_HEADING.match(line):
            break
        match = _LABEL_LINE.match(line)
        label = match.group("label").strip().lower() if match else None
        if label in _LABELS:
            assert match is not None  # narrowed by `label in _LABELS`
            current = _LABELS[label]
            fields.setdefault(current, [])
            value = match.group("value").strip()
            if value:
                fields[current].append(value)
            continue
        if current is not None:
            fields[current].append(line.strip())
    return {
        key: "\n".join(part for part in parts if part).strip()
        for key, parts in fields.items()
        if any(part.strip() for part in parts)
    }


# --------------------------------------------------------------------------
# "What changed" extraction
# --------------------------------------------------------------------------

_DOD_EVIDENCE_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:dod[ _-]?evidence|evidence|definition of done)\b.*$",
    re.IGNORECASE,
)
_DOD_EVIDENCE_KEY = re.compile(r"^\s*\**\s*dod[_ -]?evidence\s*\**\s*:", re.IGNORECASE)
_SKIP_PREFIXES = ("<!--", "|", ">", "```", "- [ ]", "- [x]", "* [ ]", "* [x]")


def _paragraphs(lines: Iterable[str]) -> list[str]:
    """Group consecutive prose lines into paragraphs, dropping non-prose lines."""
    paragraphs: list[str] = []
    buffer: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or _HEADING.match(raw) or line.startswith(_SKIP_PREFIXES):
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer))
    return paragraphs


def _section_after(body: str, heading: re.Pattern[str]) -> str:
    """Return the first prose paragraph under the first heading matching."""
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        if heading.match(line):
            tail: list[str] = []
            for following in lines[index + 1 :]:
                if _HEADING.match(following):
                    break
                tail.append(following)
            paragraphs = _paragraphs(tail)
            if paragraphs:
                return paragraphs[0]
    return ""


def _inline_key_value(body: str, key: re.Pattern[str]) -> str:
    """Return the value of an inline ``key: value`` line, if present."""
    for line in (body or "").splitlines():
        if key.match(line):
            _, _, value = line.partition(":")
            if value.strip():
                return value.strip()
    return ""


def what_changed_candidates(pull_request: ModelMergedPullRequest) -> list[str]:
    """Ordered candidates for the one-paragraph "what changed" field.

    dod_evidence first because the receipt gate already forces it to exist and
    to be about this change; a free prose paragraph is a weaker but still real
    source; the title is the floor, and is never empty because the pr-title gate
    requires it.
    """
    body = pull_request.body or ""
    candidates = [
        _inline_key_value(body, _DOD_EVIDENCE_KEY),
        _section_after(body, _DOD_EVIDENCE_HEADING),
    ]
    candidates.extend(_paragraphs(body.splitlines())[:1])
    candidates.append(pull_request.title)
    return [candidate for candidate in candidates if candidate.strip()]


def _first_clean(candidates: Sequence[str]) -> tuple[str, bool]:
    """Return the first candidate free of internal references.

    The bool is True when at least one candidate was rejected, so the caller can
    name the redaction instead of silently degrading to a weaker source.
    """
    redacted = False
    for candidate in candidates:
        if contains_internal_reference(candidate):
            redacted = True
            continue
        return candidate.strip(), redacted
    return "", redacted


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_NOT_SUPPLIED = (
    "Not supplied by the merging lane. Ask before probing — this note will not "
    "guess a probe it cannot derive from the diff."
)
_WITHHELD = (
    "Withheld: the source text referenced an internal-only surface. Ask the "
    "merging lane for a version you can act on."
)
_NOTE_KEY_LABEL = "integration-note-key"


def note_key(pull_request: ModelMergedPullRequest) -> str:
    """Stable idempotency key for one merge: ``owner/repo#number``."""
    return f"{pull_request.repo}#{pull_request.number}"


def render_pin_recipe(
    pull_request: ModelMergedPullRequest, roster: ModelContractorRoster
) -> str:
    """Render the dev-only pin command for this repo from the roster overlay."""
    recipe = roster.repo_pin_recipes.get(pull_request.repo, roster.default_pin_recipe)
    _, _, repo_name = pull_request.repo.partition("/")
    return recipe.template.format(
        repo=pull_request.repo,
        repo_name=repo_name or pull_request.repo,
        repo_url=f"https://github.com/{pull_request.repo}",
        merge_sha=pull_request.merge_sha,
    )


def _render_reachability(
    pull_request: ModelMergedPullRequest,
    roster: ModelContractorRoster,
    release_tags: Sequence[str],
) -> tuple[str, EnumReachability]:
    if release_tags:
        tags = sorted(release_tags)
        head = f"Released. First tag containing this commit: {tags[0]}."
        if len(tags) > 1:
            head += f" (also in: {', '.join(tags[1:])})"
        return head, EnumReachability.RELEASED
    body = (
        f"Not in a released tag yet — this is on `{pull_request.base_ref}` only. "
        "To reach it before the next release, pin the merge commit:\n\n"
        f"    {render_pin_recipe(pull_request, roster)}"
    )
    return body, EnumReachability.DEV_ONLY


def _render_surfaces(authored: str, contractor: ModelContractorRosterEntry) -> str:
    if authored:
        return authored
    if contractor.surfaces:
        return (
            "Not narrowed by the merging lane. Surfaces you own that this merge "
            f"may touch: {', '.join(contractor.surfaces)}."
        )
    return "Not narrowed by the merging lane, and no surfaces are configured for you."


def compose_integration_note(
    *,
    pull_request: ModelMergedPullRequest,
    ticket: ModelTicketFacts | None,
    roster: ModelContractorRoster,
    release_tags: Sequence[str],
    existing_note_keys: Sequence[str],
) -> ModelIntegrationNoteDecision:
    """Decide whether a note is owed for this merge and render it.

    Pure. Returns a skip decision with a named reason whenever no note is owed;
    a skip is a normal outcome, not a failure, and every skip records which of
    the exhaustive reasons applied.
    """
    key = note_key(pull_request)

    if ticket is None:
        return ModelIntegrationNoteDecision(
            should_post=False,
            skip_reason=EnumNoteSkipReason.TICKET_NOT_FOUND
            if extract_ticket_reference(pull_request)
            else EnumNoteSkipReason.NO_TICKET_REFERENCE,
            note_key=key,
        )

    if ticket.assignee_linear_user_id is None:
        return ModelIntegrationNoteDecision(
            should_post=False,
            skip_reason=EnumNoteSkipReason.TICKET_UNASSIGNED,
            note_key=key,
            ticket_identifier=ticket.identifier,
        )

    contractor = match_contractor(ticket.assignee_linear_user_id, roster)
    if contractor is None:
        return ModelIntegrationNoteDecision(
            should_post=False,
            skip_reason=EnumNoteSkipReason.ASSIGNEE_NOT_CONTRACTOR,
            note_key=key,
            ticket_identifier=ticket.identifier,
        )

    if key in set(existing_note_keys):
        return ModelIntegrationNoteDecision(
            should_post=False,
            skip_reason=EnumNoteSkipReason.ALREADY_POSTED,
            note_key=key,
            ticket_identifier=ticket.identifier,
            recipient_display_name=contractor.display_name,
        )

    authored = extract_authored_fields(pull_request.body)
    redacted: list[str] = []

    what_changed_sources = (
        [authored["what_changed"], *what_changed_candidates(pull_request)]
        if authored.get("what_changed")
        else what_changed_candidates(pull_request)
    )
    what_changed, what_changed_redacted = _first_clean(what_changed_sources)
    if what_changed_redacted:
        redacted.append("what_changed")
    if not what_changed:
        what_changed = _WITHHELD

    def _authored_or(field: str, fallback: str) -> str:
        value = authored.get(field, "")
        if not value:
            return fallback
        if contains_internal_reference(value):
            redacted.append(field)
            return _WITHHELD
        return value

    surfaces = _render_surfaces(_authored_or("surfaces", ""), contractor)
    probe = _authored_or("probe", _NOT_SUPPLIED)
    pass_expectation = _authored_or("pass_expectation", _NOT_SUPPLIED)
    reachable, reachability = _render_reachability(pull_request, roster, release_tags)

    pr_title, title_redacted = _first_clean([pull_request.title])
    if title_redacted:
        redacted.append("pr_title")
        pr_title = _WITHHELD

    merged_at = pull_request.merged_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "\n".join(
        [
            f"**INTEGRATION NOTE — {ticket.identifier}**",
            "",
            f"_For {contractor.display_name}. Posted automatically when this "
            "merge landed; no action was needed from you to receive it._",
            "",
            "**What changed**",
            what_changed,
            "",
            "**What it means for your surfaces**",
            surfaces,
            "",
            "**Probe to run**",
            probe,
            "",
            "**Pass expectation**",
            pass_expectation,
            "",
            "**Reachable when**",
            reachable,
            "",
            "**Delivery facts**",
            "",
            f"- Repo: `{pull_request.repo}`",
            f"- PR: [#{pull_request.number}]({pull_request.html_url}) — {pr_title}",
            f"- Merged: {merged_at} into `{pull_request.base_ref}`",
            f"- Merge commit: `{pull_request.merge_sha}`",
            "",
            f"{_NOTE_KEY_LABEL}: {key}",
        ]
    )

    return ModelIntegrationNoteDecision(
        should_post=True,
        skip_reason=None,
        note_key=key,
        note_body=body,
        reachability=reachability,
        recipient_display_name=contractor.display_name,
        ticket_identifier=ticket.identifier,
        redacted_fields=tuple(dict.fromkeys(redacted)),
    )


def parse_note_keys(comment_bodies: Iterable[str]) -> tuple[str, ...]:
    """Extract the idempotency keys already present in a ticket's comments.

    Parsing the key back out of the posted note is what makes the effect
    idempotent without a side table: the ticket itself is the record of what has
    been delivered, so a replay, a backfill dispatch, and a re-run of the same
    workflow all converge on "already posted".
    """
    pattern = re.compile(rf"^{_NOTE_KEY_LABEL}:\s*(\S+)\s*$", re.MULTILINE)
    keys: list[str] = []
    for comment in comment_bodies:
        keys.extend(pattern.findall(comment or ""))
    return tuple(dict.fromkeys(keys))


__all__ = [
    "compose_integration_note",
    "contains_internal_reference",
    "extract_authored_fields",
    "extract_ticket_reference",
    "match_contractor",
    "note_key",
    "parse_note_keys",
    "render_pin_recipe",
    "what_changed_candidates",
]
