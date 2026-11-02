# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical row grammar of the rolling work ledger.

A governed ledger is a file of rows ``<YYYY-MM-DDTHH:MM:SSZ> | <TYPE> | key=value | ... | text``.
The grammar is data (:data:`GRAMMAR`): about a dozen canonical types, each with its required
fields, and a map from the legacy spellings to the type that replaced them. The append path
refuses a NEW row of any other type or one missing a required field; legacy rows are never
rewritten or re-validated. Lane messages are MSG, ACK, HOLD and RELEASE rows with ``from=``, ``to=``
and an ``id=`` made of the row's own timestamp and the sender's lane, never a line number, because
an archive roll renumbers every line. A MSG row is never consent.

Only a ledger whose file name is in :data:`GOVERNED_LEDGER_NAMES` is judged, so other ledgers (the
JSON Lines engagement ledger, test fixtures) are untouched. Every line of a payload that OPENS a
row is judged; a continuation line is grouped with the row above it for free-text hold
instructions.

A HOLD carries a machine-checkable release condition, and another row type may not instruct a
named PR not to land in free text. An operator-standing hold (a PR only the operator may let
land) carries ``release=operator`` and ``ruling=<the stamp or stamp-lane of the row carrying the
operator's words>``; only a RELEASE naming its id frees it. This is a pattern, not understanding:
wording outside the patterns still passes, and nothing here evaluates release conditions.

This enforces shape, not truth. A row can carry every required field and still say something
false, and a quoted phrase in a RULING row is not proof the operator said it. What it removes is
the silent case: a row nobody's reader recognises, a message with no recipient, a consent row
missing the half that bounds it.

Standard library only, plus the three ledger helpers beside it. The module runs no process; its
two filesystem reads are the delegation receipt directory of a TERMINAL's lane and the one fact
:mod:`omnimarket.handlers.ledger_worktree_cell` checks for a TERMINAL's removed worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from omnimarket.handlers import ledger_claim_contract as claim_contract
from omnimarket.handlers import ledger_worktree_cell as worktree_cell
from omnimarket.handlers.ledger_accounting_cells import accounting_cells

__all__ = [
    "CANONICAL_TYPES",
    "GOVERNED_LEDGER_NAMES",
    "GRAMMAR",
    "GRAMMAR_VERSION",
    "LEGACY_TYPE_MAP",
    "TOOL_INTERNAL_TYPES",
    "delegation_cell_reason",
    "governs",
    "grammar_json",
    "hold_instruction_refusal",
    "is_operator_question",
    "opens_a_row",
    "operator_question_key",
    "parse_row",
    "question_key_for_row",
    "refusal_for_payload",
    "refusal_for_state",
    "row_refusals",
    "ruling_link_refusals",
]

GRAMMAR_VERSION = "ledger-row-grammar/2"

# Judged by file NAME, so the ledger keeps its grammar wherever it lives and a
# fixture named anything else is not judged.
GOVERNED_LEDGER_NAMES = frozenset({"ROLLING_WORK_LEDGER.md"})

STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
STAMP_RE = re.compile(rf"^{STAMP}$")
# A lane token: what `lane=`, `from=` and the tail of an id may hold. Colons and
# dots are in live lane names (`codex:handoff-auto-merge-guard`).
LANE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"
LANE_TOKEN_RE = re.compile(rf"^{LANE_TOKEN}$")
ID_RE = re.compile(rf"^(?P<stamp>{STAMP})-(?P<lane>{LANE_TOKEN})$")
# `re=` names a message/hold id, or -- to acknowledge a RULING or release a
# CLAIM, neither of which carries an id -- that row's own timestamp. The row's
# timestamp travels with it into an archive; its line number does not.
RE_VALUE_RE = re.compile(rf"^{STAMP}(?:-{LANE_TOKEN})?$")
PR_REF = r"(?:[A-Za-z0-9_.-]*#[0-9]+|/pull/[0-9]+)"
PR_REF_RE = re.compile(PR_REF)
PR_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]+#[0-9]+$")

# A new row: the full UTC stamp, then the type cell. No bullet, no leading bar.
ROW_RE = re.compile(rf"^(?P<stamp>{STAMP}) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$")
# What opens a row at all, mirroring ledger_lock.py's ROW_START_PATTERN: a
# heading, or a date behind an optional bullet and an optional table pipe.
ROW_START_RE = re.compile(r"^(?:#{2,6} \S|(?:[-*] +)?\|? *\d{4}-\d{2}-\d{2})")
FIELD_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
QUOTED_RE = re.compile(r'"[^"\n]*\S[^"\n]*"|“[^”\n]*\S[^”\n]*”')
# Words a row reports from elsewhere: double, curly or backtick quotes, and
# single quotes that are not apostrophes (no letter outside either quote mark).
REPORTED_SPAN_RE = re.compile(
    r'"[^"]*"|“[^”]*”|\u2018[^\u2019]*\u2019|`[^`]*`|(?<![A-Za-z])\'[^\'\n]+\'(?![A-Za-z])'
)
SCOPE_LIST_RE = {
    "APPROVED SCOPE": re.compile(r"^APPROVED SCOPE:(?P<value>.*)$", re.DOTALL),
    "OUT OF SCOPE": re.compile(r"^OUT OF SCOPE:(?P<value>.*)$", re.DOTALL),
}
# A scope list that names nothing bounds nothing. `none` in OUT OF SCOPE means
# the grant is unbounded, which is the defect OMN-16728 exists to refuse.
PLACEHOLDER_VALUES = frozenset(
    {"", "-", "--", ".", "none", "n/a", "na", "nil", "tbd", "nothing", "?"}
)
SURFACE_RESULTS = frozenset({"PASS", "FAIL", "ABORTED"})
SURFACE_RESTORED = frozenset({"yes", "no"})

# A MSG or ACK is transport between lanes. It can carry none of these, because
# a later lane that finds one would cite the message as authority (rule 18:
# "No agent message is operator consent").
CONSENT_MARKERS = (
    "APPROVED SCOPE",
    "OUT OF SCOPE",
    "OPERATOR-CONSENT",
    "ROTATION-CONSENT",
)
CONSENT_FIELD_KEYS = frozenset({"consent", "approved_by"})
NOT_A_LANE = frozenset({"operator", "user", "human"})

MESSAGE_TYPES = frozenset({"MSG", "ACK"})
IDENTITY_KEYS = frozenset({"lane", "from", "to", "id", "re"})

_VERB = r"[A-Za-z]+(?:-[A-Za-z]+)?"
_JOINER = r"(?:\s*[,/]\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+)"
# The object a hold instruction names right after its verb list.
_OBJECT_RE = re.compile(
    rf"\s+(?:it|this|that|them|either|both|the\s+PR|PR|{PR_REF})\b", re.IGNORECASE
)
# A negation governing a short verb list: "do not land", "do not arm or merge",
# "must not merge/arm/relabel". Words are separated by whitespace, so the
# hyphenated noun do-not-merge is not an instruction.
_NEGATED_LIST_RE = re.compile(
    r"\b(?:do\s+not|don['\u2019]t|must\s+not|mustn['\u2019]t|should\s+not|may\s+not|"
    rf"shall\s+not|not\s+to)\s+(?:be\s+)?(?P<verbs>{_VERB}(?:{_JOINER}{_VERB}){{0,4}})",
    re.IGNORECASE,
)
_LANDING_VERBS = frozenset({"land", "merge", "landed", "merged"})
# A list that also names one of these is a lane's conduct ("Do not merge, arm
# or rerun", "Codex must not merge/arm/relabel"): the one-lane-lands-PRs rule
# restated, which holds no PR unless an object follows the list.
_CONDUCT_VERBS = frozenset(
    {
        "arm",
        "rearm",
        "disarm",
        "rerun",
        "relabel",
        "label",
        "ready-flip",
        "flip",
        "release",
        "poll",
        "push",
        "force-push",
        "touch",
        "approve",
        "close",
        "rebase",
        "retag",
    }
)
_NOT_BE_LANDED_RE = re.compile(r"\bnot\s+be\s+(?:landed|merged)\b", re.IGNORECASE)
_HOLD_OFF_RE = re.compile(
    r"\bhold\s+off\s+(?:on\s+)?(?:landing|merging)\b", re.IGNORECASE
)
_NEVER_LAND_OBJECT_RE = re.compile(
    rf"\bnever\s+(?:land|merge)\s+(?:it|this|that|them|either|both|the\s+PR|PR|{PR_REF})\b",
    re.IGNORECASE,
)
# A present or imperative landing verb, up to three tokens, then an ordering
# word and a PR. The tokens in between may not cross a cell or a sentence
# (no ; | . : or parentheses), so "no runtime merge this cycle; after #4096
# lands" is two clauses, not an ordering.
_ORDERED_LANDING_RE = re.compile(
    rf"\b(?:land|lands|landing|merge|merges|merging)\b"
    rf"(?:\s+[^\s;|.:()]+){{0,3}}\s+(?:after|before|alongside|together\s+with)\s+"
    rf"(?:PR\s+)?{PR_REF}\b",
    re.IGNORECASE,
)
# "landing order" as a noun in a report is not an instruction; a "LANDING
# ORDER:" header that opens a sequence is.
_IN_ORDER_RE = re.compile(
    r"\b(?:land(?:s|ing)?|merge)[ -]+in[ -]+order\b|\border\s+matters\b"
    r"|\b(?:landing|merge)\s+order\s*:",
    re.IGNORECASE,
)
_HOLD_INSTRUCTION_PATTERNS = (
    _NOT_BE_LANDED_RE,
    _HOLD_OFF_RE,
    _NEVER_LAND_OBJECT_RE,
    _ORDERED_LANDING_RE,
    _IN_ORDER_RE,
)


def _negated_landing(text: str) -> re.Match[str] | None:
    """The first negation whose verb list forbids landing a PR, or None. A
    conduct list (landing named beside arming, rerunning, relabelling...) counts
    only when a PR or a pronoun for one follows the list."""
    for match in _NEGATED_LIST_RE.finditer(text):
        verbs = {v.lower() for v in re.split(r"[\s,/]+", match.group("verbs")) if v}
        verbs -= {"or", "and"}
        if not verbs & _LANDING_VERBS:
            continue
        if verbs & _CONDUCT_VERBS and _OBJECT_RE.match(text, match.end()) is None:
            continue
        return match
    return None


# Written by ledger_lock.py itself (the replay marker, the stranded-clone
# note), never through --append. A caller that writes one is refused.
TOOL_INTERNAL_TYPES = frozenset({"REPLAY", "STRANDED-CLONE NOTE"})

GRAMMAR: dict[str, dict[str, object]] = {
    "CLAIM": {
        "means": "I am about to do this work; nobody else should start it.",
        "required": ["lane", "ticket|tickets", "worktree"],
        "optional": [
            "actor",
            "model",
            "scope",
            "repo",
            "pr",
            "consent",
            "ruling",
            "follows",
            "parent",
            "related",
            "contract-schema",
            "dod-evidence",
        ],
        "rules": [
            "carries actor= or model= and a rule-4 cost sentence 'est ~N lane-hours; displaces "
            "X; (OMN-n)' (existing ledger_lock CLAIM guards, unchanged)",
            "consent-bound work cites its OPERATOR-CONSENT row with consent=",
            "worktree=<path> or worktree=none, its own cell (OMN-20148); a script or launchd "
            "lane (actor=script:*, actor=launchd:*, model=none) is exempt",
            "when ONEX_LEDGER_REQUIRE_CLAIM_CONTRACT is on (default OFF), contract-schema="
            "<major.minor.patch> and dod-evidence=<non-empty JSON array> with a "
            "behavior-proving item are required (OMN-20029); exactly one actor= cell naming "
            "script:landing-controller, script:onex-remote-lane, or "
            "launchd:morning-worktree-prune is exempt",
        ],
    },
    "STATUS": {
        "means": "Where my lane is: progress, a finding, an action taken, a PR held, evidence.",
        "required": ["lane"],
        "optional": [
            "ticket",
            "repo",
            "pr",
            "head",
            "state",
            "claim",
            "countersigns",
            "verdict",
            "relation",
            "subject",
            "object",
        ],
        "rules": [
            "a countersign of another lane's receipt carries countersigns=<receipt> and verdict="
        ],
    },
    "TERMINAL": {
        "means": "My lane is done with this claim; here is the outcome and evidence.",
        "required": ["lane", "friction"],
        "optional": [
            "ticket",
            "closes",
            "closes-CLAIM",
            "outcome",
            "pr",
            "squash",
            "worktree",
        ],
        "rules": [
            "friction=none, or friction=<one line> naming this lane's FRICTION row(s), which "
            "must exist (friction guard, OMN-18274)",
            "delegated=<n> with runs=, codex= or jev= ids (n >= 1), or delegated=0 "
            "with delegation_reason=<why> (lane-authored:<why> plus a pr= or diff= cell when the "
            "lane wrote the code itself) or a parenthesised (why), or delegation=na:<reason>; "
            "lab=<n> or glm=<n> (n >= 1) needs runs= ids; "
            "actor=script:*, actor=launchd:*, or model=none is exempt (OMN-17427)",
            "worktree=removed:<path>, pinned-removed:<path>@<archive>, kept:<path>:<reason> or "
            "none is required when the CLAIM it closes named a worktree path; removed and "
            "pinned-removed are refused while the path still exists (OMN-20148)",
        ],
    },
    "FRICTION": {
        "means": "Process friction this lane hit, with the ticket that owns it and what it cost.",
        "required": ["lane", "ticket|tickets|existing"],
        "optional": ["cost", "class", "related"],
        "rules": [
            "the ticket value is an OMN id and the row carries a cost estimate (friction "
            "guard, OMN-18274)"
        ],
    },
    "CORRECTION": {
        "means": "A row already on the ledger was wrong or incomplete; this is the fix.",
        "required": ["lane", "corrects|amends|supersedes"],
        "optional": ["ticket", "cost", "operator_span"],
        "rules": [
            "name the corrected row by its timestamp where you can; a line number moves when "
            "the ledger rolls",
            "operator_span=<start>:<end> (with corrects=) marks the characters of the target "
            "row's first quoted cell that are the operator's words; it must lie inside that "
            "quote, and a target not on the ledger refuses it (OMN-18531, TA.4)",
        ],
    },
    "RULING": {
        "means": "An operator decision, recorded with the operator's own words.",
        "required": ["lane", "question", '"<operator words>"'],
        "optional": [
            "ticket",
            "amends",
            "extends",
            "supersedes",
            "approved_by",
            "prompt_id",
            "covers",
            "prompt_match",
            "confirms",
            "confirmed_by",
            "reviewed",
        ],
        "rules": [
            "question= names the question or options the operator's words answer; "
            "never empty or a placeholder",
            "prompt_id=<uuid> with covers=<start>:<end> link the row to the captured operator "
            "prompt whose text holds the words; prompt_match=ambiguous marks several matches "
            "and carries neither; confirms=<sha256> with confirmed_by=<uuid> mark a confirmed "
            "candidate and need the link; reviewed=<first uuid>..<last uuid> names prompts "
            "the operator reviewed with nothing further to record (OMN-18531, TA.4)",
            "the operator's words appear verbatim in double quotes",
            "a second RULING on a subject with an unacknowledged one is refused (ruling guard, "
            "OMN-18258)",
        ],
    },
    "OPERATOR-CONSENT": {
        "means": (
            "The operator consented to a gated action; this row is the durable authorization "
            "(rule 18)."
        ),
        "required": [
            "lane",
            '"<operator words>"',
            "APPROVED SCOPE: <list>",
            "OUT OF SCOPE: <list>",
        ],
        "optional": [
            "approved_by",
            "ticket",
            "prompt_id",
            "covers",
            "prompt_match",
            "confirms",
            "confirmed_by",
        ],
        "rules": [
            "the prompt link fields are the RULING row's (OMN-18531, TA.4)",
            "APPROVED SCOPE: and OUT OF SCOPE: each open their own cell and name something (-, "
            "none, n/a are refused)",
            "a credential rotation also carries approved_by=<operator|jake> (rule 22)",
        ],
    },
    "MSG": {
        "means": "A message from one lane to another lane, to all lanes, or to the operator.",
        "required": ["from", "to", "id"],
        "optional": ["ticket", "repo", "pr", "re"],
        "rules": [
            "id=<this row's timestamp>-<from lane>",
            "to=<lane>, a comma list of lanes, all, or operator",
            "never consent-shaped: no APPROVED SCOPE, OUT OF SCOPE, OPERATOR-CONSENT, "
            "ROTATION-CONSENT, consent= or approved_by=, and from= is a lane, never the "
            "operator",
            "lane=, if present, equals from=",
        ],
    },
    "ACK": {
        "means": "I received the message, hold or ruling named by re=.",
        "required": ["from", "to", "id", "re"],
        "optional": ["ticket"],
        "rules": [
            "id=<this row's timestamp>-<from lane>",
            "re=<id> of the MSG or HOLD, or re=<timestamp> of the RULING being acknowledged",
            "never consent-shaped, as for MSG",
        ],
    },
    "HOLD": {
        "means": "Stop: nobody proceeds on this scope until a RELEASE names this hold.",
        "required": ["lane", "id", "to|repo|pr|surface"],
        "optional": [
            "ticket",
            "after",
            "until",
            "proof",
            "release",
            "ruling",
            "reason",
        ],
        "rules": [
            "id=<this row's timestamp>-<lane>",
            "carries at least one release condition: after=<PR[,PR...]>, until=<UTC "
            "timestamp>, proof=<row id[,row id...]>, or release=operator with "
            "ruling=<row stamp or id>",
            "a hold on a proof surface (surface=) also carries until=<UTC timestamp>; this "
            "replaces the LEASE row",
            "a surface HOLD is refused inside the lock while another lane holds that surface: "
            "a HOLD no RELEASE re=<its id> names, whose until= is later than the new row's "
            "timestamp (OMN-19261)",
        ],
    },
    "RELEASE": {
        "means": "The hold (or claim) named by re= is released.",
        "required": ["lane", "re"],
        "optional": ["ticket", "surface", "pr", "result", "restored", "reason"],
        "rules": [
            "re=<id> of the HOLD, or re=<timestamp> of the CLAIM being given up",
            "a surface release (surface=) also carries result=<PASS|FAIL|ABORTED> and "
            "restored=<yes|no>",
            "a RELEASE of a HOLD id with pr=<repo#n[,repo#n...]> and no surface= is a "
            "SCOPED release (OMN-20956): it frees only the named PRs, and the hold stays "
            "in force for every other PR it covers; a RELEASE of the id with no pr= frees "
            "the whole hold. Several scoped releases of one id add up, and the hold is "
            "released once its own pr= list is covered. Readers take the scope from "
            "release_scope() in this module",
        ],
    },
}
CANONICAL_TYPES = frozenset(GRAMMAR)

# Every legacy type with more than a handful of rows, mapped to the canonical
# type a new row of that kind uses, measured over the live ledger plus its archive
# splits on 2026-09-23.
LEGACY_TYPE_MAP: dict[str, str] = {
    "NOTE": "STATUS",
    "PROGRESS": "STATUS",
    "ACTION": "STATUS",
    "FINDING": "STATUS",
    "OBSERVATION": "STATUS",
    "TRIAGE": "STATUS",
    "CHECKPOINT": "STATUS",
    "EVIDENCE": "STATUS",
    "COUNTERSIGN": "STATUS",
    "COUNTERSIGN-REVISED": "STATUS",
    "HELD": "STATUS",
    "MERGED": "STATUS",
    "SEND": "STATUS",
    "SLACK-SENT": "STATUS",
    "TICKET-MINTED": "STATUS",
    "TICKET": "STATUS",
    "BLOCKED": "STATUS",
    "BLOCKER": "STATUS",
    "CLAIM_UPDATE": "STATUS",
    "ADDENDUM": "STATUS",
    "UPDATE": "STATUS",
    "DECISION-LINK": "STATUS",
    "INVENTORY": "STATUS",
    "RESULT": "STATUS",
    "RECONCILIATION": "STATUS",
    "RERUN_PENDING": "STATUS",
    "CLEANUP": "STATUS",
    "REVIEW": "STATUS",
    "REVIEW_FINDING": "STATUS",
    "DIAGNOSIS": "STATUS",
    "READBACK": "STATUS",
    "RECORD": "STATUS",
    "DONE": "TERMINAL",
    "CLOSEOUT": "TERMINAL",
    "TERMINAL HOLD": "TERMINAL",
    "TERMINAL-FAILURE": "TERMINAL",
    "LANE-TERMINAL-STATE": "TERMINAL",
    "CLOSE_SUPERSEDED": "TERMINAL",
    "CLAIM-EXTENSION": "CLAIM",
    "CLAIM-AMEND": "CLAIM",
    "LEASE": "HOLD",
    "REVIEW_HOLD": "HOLD",
    "COORDINATION": "MSG",
    "ASSIGNMENT": "MSG",
    "STATUS_REQUEST": "MSG",
    "HANDOFF": "MSG",
    "HANDOFF_REQUEST": "MSG",
    "QUESTION": "MSG",
    "ANSWER": "MSG",
    "DECISION-REQUESTED": "MSG",
    "REVIEW_REQUEST": "MSG",
    "REVIEW_APPROVE": "MSG",
    "FRICTION-CORRECTION": "CORRECTION",
    "TERMINAL-ADDENDUM": "CORRECTION",
    "NOTE-CORRECTION": "CORRECTION",
    "OPERATOR-RULING": "RULING",
    "DECISION": "RULING",
}


class Row:
    """One stamped row, split into its type and its fields.

    A plain class, not a dataclass, so that a loader which imports this module by path
    (where `sys.modules[cls.__module__]` is absent) can still resolve it.
    """

    __slots__ = ("cells", "fields", "row_type", "stamp", "text")

    def __init__(self, text: str, stamp: str, row_type: str, cells: list[str]) -> None:
        self.text = text
        self.stamp = stamp
        self.row_type = row_type
        self.cells = cells
        self.fields: dict[str, list[str]] = {}
        for cell in cells:
            match = FIELD_RE.match(cell)
            if match:
                self.fields.setdefault(match.group("key").lower(), []).append(
                    match.group("value").strip()
                )

    def value(self, key: str) -> str | None:
        """The first non-empty value of `key`, or None."""
        for value in self.fields.get(key, []):
            if value:
                return value
        return None

    def token(self, key: str) -> str | None:
        """The value of an identity field, only when its cell is exactly
        `key=<one token>`. `lane=x actor=y` in one cell is not a lane field a
        reader can parse (the claim index and ruling guard both read the whole
        cell), so it does not count."""
        for cell in self.cells:
            match = FIELD_RE.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None


def governs(ledger_name: str) -> bool:
    """Whether the ledger at this path or name is judged by this grammar."""
    return ledger_name.replace("\\", "/").rsplit("/", 1)[-1] in GOVERNED_LEDGER_NAMES


def opens_a_row(line: str) -> bool:
    return ROW_START_RE.match(line) is not None


def parse_row(line: str, *, types: frozenset[str] | None = None) -> Row | None:
    """The row, or None when `line` is not in the canonical row shape.

    With `types`, a row of any other type is also None, before its cells are split and
    its fields indexed: a reader that needs only a few row types over a ledger of tens
    of thousands of rows pays for those rows alone."""
    match = ROW_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    if types is not None and match.group("type").strip() not in types:
        return None
    rest = match.group("rest")
    cells = [cell.strip() for cell in rest.split("|")] if rest is not None else []
    return Row(line, match.group("stamp"), match.group("type").strip(), cells)


def _pr_refs(row: Row, key: str) -> frozenset[str]:
    """The `repo#n` handles in every `key=` cell of `row`, lower-cased and comma-split."""
    return frozenset(
        item.strip().lower()
        for value in row.fields.get(key, [])
        for item in value.split(",")
        if item.strip()
    )


def release_scope(release: Row) -> frozenset[str] | None:
    """The PRs a RELEASE frees, or None when it frees the whole hold named by re= (OMN-20956).

    Only a RELEASE of a hold id (`re=<timestamp>-<lane>`) that carries `pr=` and no
    `surface=` is scoped; a release of a claim (`re=<timestamp>`) or of a proof-surface
    hold keeps its `pr=` as information. A scoped release whose `pr=` holds nothing
    readable frees no PR at all: an unreadable scope never widens into a full release."""
    if release.row_type != "RELEASE" or release.value("surface") is not None:
        return None
    re_value = release.token("re")
    if re_value is None or ID_RE.match(re_value) is None:
        return None
    if release.value("pr") is None:
        return None
    return frozenset(ref for ref in _pr_refs(release, "pr") if PR_VALUE_RE.match(ref))


def hold_release_state(
    hold: Row, releases: Iterable[Row]
) -> tuple[bool, frozenset[str]]:
    """`(released, freed)` for one HOLD against the RELEASE rows that may name it.

    `released` is True once a plain RELEASE re=<its id> exists, or once scoped releases
    cover every PR in the hold's own `pr=` list. `freed` is the PRs scoped releases freed
    while the hold stays in force for the rest (a repo-wide or lane-wide hold has no PR
    list, so scoped releases never end it). A hold with no id cannot be named by a release."""
    hold_id = hold.token("id")
    if hold_id is None:
        return False, frozenset()
    freed: set[str] = set()
    for release in releases:
        if release.row_type != "RELEASE" or release.token("re") != hold_id:
            continue
        scope = release_scope(release)
        if scope is None:
            return True, frozenset(freed)
        freed |= scope
    covered = _pr_refs(hold, "pr")
    return bool(covered) and covered <= freed, frozenset(freed)


def _has_any(row: Row, alternatives: str) -> bool:
    return any(row.value(key) for key in alternatives.split("|"))


def _type_refusal(row_type: str) -> str:
    allowed = " ".join(sorted(CANONICAL_TYPES))
    upper = row_type.upper()
    if row_type in TOOL_INTERNAL_TYPES:
        return f"row type {row_type!r} is written only by onex-ledger itself. Allowed types: {allowed}"
    if upper in CANONICAL_TYPES:
        return (
            f"row type {row_type!r} must be spelled {upper!r}. Allowed types: {allowed}"
        )
    mapped = LEGACY_TYPE_MAP.get(upper)
    hint = (
        f"{row_type!r} is a legacy type; write it as {mapped}"
        if mapped
        else f"{row_type!r} is not a row type"
    )
    return f"{hint}. Allowed types: {allowed}"


def _id_refusal(row: Row, owner_key: str) -> str | None:
    value = row.token("id")
    if value is None:
        return f"missing id= (its own cell, id={row.stamp}-<{owner_key} lane>)"
    match = ID_RE.match(value)
    if match is None:
        return (
            f"id={value!r} is not <timestamp>-<lane>; an id is this row's own timestamp "
            f"and the sender's lane (id={row.stamp}-<{owner_key} lane>), never a line number"
        )
    owner = row.token(owner_key)
    if match.group("stamp") != row.stamp:
        return f"id={value!r} does not carry this row's own timestamp {row.stamp}"
    if owner is not None and match.group("lane") != owner:
        return f"id={value!r} does not end in this row's {owner_key}={owner}"
    return None


def _re_refusal(row: Row) -> str | None:
    value = row.token("re")
    if value is None:
        return "missing re= (its own cell): the id or timestamp of the row this answers"
    if RE_VALUE_RE.match(value) is None:
        return (
            f"re={value!r} is not an id (<timestamp>-<lane>) or a row timestamp; "
            "a line number or claim token is refused because an archive roll renumbers lines"
        )
    return None


def _to_refusal(row: Row) -> str | None:
    value = row.token("to")
    if value is None:
        return "missing to= (its own cell): a lane, a comma list of lanes, all, or operator"
    if not all(LANE_TOKEN_RE.match(part) for part in value.split(",")):
        return f"to={value!r} is not a lane, a comma list of lanes, all, or operator"
    return None


def _quoted_refusal(row: Row) -> str | None:
    # A quoted question supplies context, never the operator's answer (OMN-20656).
    text = " | ".join(c for c in row.cells if not re.match(r"(?i)^question=", c))
    if QUOTED_RE.search(text):
        return None
    return 'the operator\'s words are not quoted verbatim in double quotes ("...")'


def _scope_list_refusals(row: Row) -> list[str]:
    reasons: list[str] = []
    for name, pattern in SCOPE_LIST_RE.items():
        values = [
            m.group("value").strip() for c in row.cells if (m := pattern.match(c))
        ]
        if not values:
            reasons.append(
                f"missing a cell opening with '{name}:' (OMN-16728, rule 18)"
            )
        elif all(v.lower().rstrip(".") in PLACEHOLDER_VALUES for v in values):
            reasons.append(
                f"'{name}:' names nothing ({values[0]!r}); a scope list that names nothing "
                "bounds nothing (OMN-16728, rule 18)"
            )
    return reasons


def _consent_shape_refusals(row: Row) -> list[str]:
    reasons: list[str] = []
    for marker in CONSENT_MARKERS:
        if marker in row.text:
            reasons.append(
                f"a {row.row_type} row may not carry {marker!r}: a message is never consent "
                "(rule 18); record operator consent as its own OPERATOR-CONSENT row"
            )
    for key in sorted(CONSENT_FIELD_KEYS & set(row.fields)):
        reasons.append(
            f"a {row.row_type} row may not carry {key}=: a message carries no authority; "
            "cite consent in your CLAIM row"
        )
    sender = row.token("from")
    if sender is not None and sender.lower() in NOT_A_LANE:
        reasons.append(
            f"from={sender} is not a lane; operator words are recorded as a RULING or "
            "OPERATOR-CONSENT row, never as a message"
        )
    return reasons


def hold_instruction_refusal(row_text: str, row_type: str) -> str | None:
    """Refuse a named PR hold expressed as prose instead of a HOLD row."""
    if row_type not in CANONICAL_TYPES or row_type in {
        "HOLD",
        "RULING",
        "OPERATOR-CONSENT",
    }:
        return None
    if PR_REF_RE.search(row_text) is None:
        return None

    unquoted = REPORTED_SPAN_RE.sub(" ", row_text)
    matches = [pattern.search(unquoted) for pattern in _HOLD_INSTRUCTION_PATTERNS]
    matches.append(_negated_landing(unquoted))
    matched = min(
        (match for match in matches if match is not None),
        key=lambda match: match.start(),
        default=None,
    )
    if matched is None:
        return None
    phrase = matched.group(0)
    return (
        f"{row_type} row matched {phrase!r}: free text cannot hold a PR because nothing can "
        "release it. Write a HOLD row instead: <ts> | HOLD | lane=<lane> | "
        "id=<ts>-<lane> | pr=<repo>#<n> | after=<repo>#<n> or until=<UTC ts> or "
        "proof=<row id> | reason. Words it is reporting from another row go in double "
        "quotes or are cited by that row's id"
    )


def _comma_values_refusal(row: Row, key: str, pattern: re.Pattern[str]) -> str | None:
    values = row.fields.get(key, [])
    for value in values:
        if not value or not all(pattern.fullmatch(item) for item in value.split(",")):
            return f"{key}={value!r} is malformed"
    return None


# `delegated=0 (why)`: the parenthesised reason onex-delegation-accounting also reads.
_PAREN_REASON_AFTER_ZERO = re.compile(
    r"(?:^|[\s|])delegated=0\b[^|(]{0,40}\(\s*[^()\s][^()]*\)"
)
_DELEGATION_NA_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_DELEGATION_NA_PLACEHOLDERS = frozenset({"-", "none", "n/a", "x"})
_LANE_AUTHORED_PREFIX = "lane-authored:"


def _delegation_na_value(row: Row, cells: dict[str, str]) -> str | None:
    """Return a typed NA value, retaining a direct cell's complete value.

    The strict token reader intentionally ends values at whitespace. A direct
    ``delegation=na:two words`` cell therefore needs the row-level value to
    reject its embedded space rather than accepting ``two`` as its reason.
    """
    for cell in row.cells:
        match = FIELD_RE.match(cell)
        if match and match.group("key").lower() == "delegation":
            return match.group("value").strip()
    return cells.get("delegation")


def _runs_ids(cells: dict[str, str]) -> bool:
    """Whether runs= carries one or more non-placeholder identifiers."""
    runs = cells.get("runs", "").strip(" ,;.")
    return bool(runs and runs.lower() != "none" and all(runs.split(",")))


def _governed_terminal_row(line: str) -> Row | None:
    """The TERMINAL row, unless its identity exempts it from delegation checks."""
    row = parse_row(line)
    if row is None:
        return None
    kind = row.row_type.upper()
    if LEGACY_TYPE_MAP.get(kind, kind) != "TERMINAL":
        return None
    identities = dict(re.findall(r"(?:^|[\s|])(actor|model)=([^\s|]*)", line))
    if (
        identities.get("actor", "").startswith(("script:", "launchd:"))
        or identities.get("model") == "none"
    ):
        return None
    return row


def _declared_cells(line: str) -> dict[str, str]:
    """Keep the first non-empty declared accounting value for each key."""
    cells: dict[str, str] = {}
    for key, value in accounting_cells(line):
        if not cells.get(key):
            cells[key] = value
    return cells


def _lane_authored_reason(row: Row, cells: dict[str, str]) -> str | None:
    """Receipt checks for `delegation_reason=lane-authored:<why>` (OMN-20909).

    A lane that wrote code itself with no delegation route has no other honest
    cell. The reason carries a non-empty why and the row cites the PR or diff
    the lane wrote.
    """
    reason = cells.get("delegation_reason", "").strip(" ,;.")
    if not reason.startswith(_LANE_AUTHORED_PREFIX.rstrip(":")):
        return None
    if (
        not reason.startswith(_LANE_AUTHORED_PREFIX)
        or not reason[len(_LANE_AUTHORED_PREFIX) :].strip()
    ):
        return (
            "delegation_reason=lane-authored needs a why: "
            "`delegated=0 delegation_reason=lane-authored:<why>` (OMN-20909)"
        )
    if not any(row.fields.get(key) for key in ("pr", "diff")):
        return (
            "delegation_reason=lane-authored:<why> needs the PR or diff the lane wrote cited "
            "in a pr= or diff= cell (OMN-20909)"
        )
    return None


def _delegation_shape_reason(line: str) -> str | None:
    """Require explicit delegation accounting on new human/agent TERMINAL rows."""
    row = _governed_terminal_row(line)
    if row is None:
        return None
    cells = _declared_cells(line)
    counts: dict[str, int] = {}
    for key in ("lab", "glm"):
        if key not in cells:
            continue
        try:
            count = int(cells[key].rstrip(",;."))
        except ValueError:
            return f"{key}={cells[key]!r} is not an integer count"
        if count < 0:
            return f"{key}={cells[key]!r} is not a non-negative integer count"
        counts[key] = count
    if any(count >= 1 for count in counts.values()) and not _runs_ids(cells):
        return "lab=/glm= counts need the runs= ids that back them"
    na_value = _delegation_na_value(row, cells)
    if na_value is not None and na_value.startswith("na:"):
        na_reason = na_value.removeprefix("na:")
        if (
            _DELEGATION_NA_REASON_RE.fullmatch(na_reason) is None
            or na_reason in _DELEGATION_NA_PLACEHOLDERS
        ):
            return (
                "delegation=na:<reason> needs a reason matching "
                "^[a-z0-9][a-z0-9-]{1,40}$ (not a placeholder)"
            )
        return None
    try:
        delegated = int(cells.get("delegated", "").rstrip(",;."))
    except ValueError:
        delegated = -1
    if delegated == 0 and (
        cells.get("delegation_reason", "").strip(" ,;.")
        or _PAREN_REASON_AFTER_ZERO.search(line)
    ):
        return _lane_authored_reason(row, cells)
    # lab_agent= is interim-crush use, recorded but never a delegation cell (OMN-20299).
    engines = [cells.get(k, "").strip(" ,;.") for k in ("runs", "codex", "jev")]
    if delegated >= 1 and any(
        e and e.lower() != "none" and all(e.split(",")) for e in engines
    ):
        return None
    if any(count >= 1 for count in counts.values()):
        return None
    return (
        "TERMINAL row carries no delegation cell (OMN-17427): close with "
        "`delegated=<n> runs=<ids>` (or codex=/jev= ids; lab_agent= is crush use, not delegation), "
        "`delegated=0 delegation_reason=<why>`, or `delegation=na:<reason>`; "
        "each is read only from a pipe cell that starts with it, never from the free text"
    )


def _undisposed_receipt_reason(line: str) -> str | None:
    """Refuse named successful delegation receipts that have no disposition."""
    row = _governed_terminal_row(line)
    if row is None:
        return None
    home = os.environ.get("OMNI_HOME")
    lane = row.token("lane")
    if not home or lane is None:
        return None
    # $OMNI_HOME/.claude_scratch/delegation-receipts is where the delegation step writes receipts.
    receipt_dir = Path(home) / ".claude_scratch" / "delegation-receipts" / lane
    if not receipt_dir.is_dir():
        return None
    named_ids = {
        identifier
        for key, value in accounting_cells(line)
        if key in ("runs", "codex", "jev")
        for identifier in value.split(",")
        if identifier and identifier != "none"
    }
    undisposed: list[str] = []
    for path in sorted(receipt_dir.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict):
            continue
        engine = receipt.get("engine")
        if (
            receipt.get("schema") != "delegation-receipt/2"
            or engine not in ("lab", "glm", "codex", "jev")
            or receipt.get("status") not in ("ok", "incumbent")
            or receipt.get("disposition")
        ):
            continue
        id_key = {
            "lab": "run_id",
            "glm": "run_id",
            "codex": "thread_id",
            "jev": "correlation_id",
        }[engine]
        identifier = receipt.get(id_key)
        if isinstance(identifier, str) and identifier in named_ids:
            undisposed.append(path.name)
    if not undisposed:
        return None
    return (
        "TERMINAL names undisposed delegation receipt(s) with no disposition (OMN-20015): "
        f"{', '.join(undisposed[:5])}: run `delegation_step.py dispose --receipt <path> "
        "--disposition used|edited|discarded|ignored --reason-code <code>` for each, "
        "then re-run `delegation_step.py cells`"
    )


def delegation_cell_reason(line: str) -> str | None:
    """Require delegation accounting and dispositions for named TERMINAL receipts."""
    reason = _delegation_shape_reason(line)
    if reason is not None:
        return reason
    return _undisposed_receipt_reason(line)


# --- OMN-18531 (TA.4): linked rulings, confirmed candidates, review ranges ---
#
# A RULING or OPERATOR-CONSENT row is linked to the captured operator prompt that holds
# its words by `prompt_id=<uuid>` and `covers=<start>:<end>` (character offsets into
# that prompt). Several matching prompts write `prompt_match=ambiguous` and no id. A
# candidate the operator confirmed carries `confirms=<sha256>` and `confirmed_by=<uuid>`
# beside the link. A RULING may carry `reviewed=<first uuid>..<last uuid>`: prompts the
# operator reviewed with nothing further to record (decision D10). A CORRECTION may
# carry `operator_span=<start>:<end>` into the target row's first quoted cell. This
# module judges shapes and the span against the ledger; whether a prompt exists in a
# session's transcript is the recorder's check, which holds the transcript.
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SPAN_RE = re.compile(r"^(?P<start>\d+):(?P<end>\d+)$")
LINK_ROW_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
LINK_KEYS = ("prompt_id", "covers", "prompt_match", "confirms", "confirmed_by")


def _single_value(row: Row, key: str) -> tuple[str | None, str | None]:
    """(value, refusal) for a key that is a field exactly once with a non-empty value."""
    values = row.fields.get(key, [])
    if len(values) != 1 or not values[0]:
        return None, f"{key}= appears once with a value ({len(values)} found)"
    return values[0], None


def _span_bounds(value: str) -> tuple[int, int] | None:
    match = SPAN_RE.match(value)
    if match is None:
        return None
    start, end = int(match.group("start")), int(match.group("end"))
    return (start, end) if start < end else None


def _link_refusals(row: Row) -> list[str]:
    """Refusals for the TA.4 fields of one row (shapes only)."""
    present = [
        key for key in (*LINK_KEYS, "reviewed", "operator_span") if key in row.fields
    ]
    if not present:
        return []
    reasons: list[str] = []
    allowed: set[str] = set()
    if row.row_type in LINK_ROW_TYPES:
        allowed.update(LINK_KEYS)
    if row.row_type == "RULING":
        allowed.add("reviewed")
    if row.row_type == "CORRECTION":
        allowed.add("operator_span")
    for key in present:
        if key not in allowed:
            reasons.append(
                f"{key}= is not a field of a {row.row_type} row (OMN-18531, TA.4)"
            )
    values: dict[str, str] = {}
    for key in present:
        if key not in allowed:
            continue
        value, reason = _single_value(row, key)
        if reason is not None:
            reasons.append(reason)
        elif value is not None:
            values[key] = value
    checks = (
        ("prompt_id", UUID_RE, "a lowercase uuid"),
        ("confirmed_by", UUID_RE, "a lowercase uuid"),
        ("confirms", SHA256_RE, "64 lowercase hex characters (a sha256)"),
    )
    for key, pattern, shape in checks:
        if key in values and pattern.match(values[key]) is None:
            reasons.append(f"{key}={values[key]!r} is malformed; expected {shape}")
    for key in ("covers", "operator_span"):
        if key in values and _span_bounds(values[key]) is None:
            reasons.append(
                f"{key}={values[key]!r} is malformed; expected <start>:<end>, "
                "non-negative integers with start before end"
            )
    if "prompt_match" in values and values["prompt_match"] != "ambiguous":
        reasons.append(
            f"prompt_match={values['prompt_match']!r} is malformed; expected ambiguous"
        )
    linked = {"prompt_id", "covers"} & set(present)
    if linked and linked != {"prompt_id", "covers"}:
        reasons.append("prompt_id= and covers= go together; neither stands alone")
    if "prompt_match" in present and linked:
        reasons.append("prompt_match=ambiguous carries no prompt_id or covers")
    confirming = {"confirms", "confirmed_by"} & set(present)
    if confirming and (confirming != {"confirms", "confirmed_by"} or len(linked) != 2):
        reasons.append(
            "confirms= needs confirmed_by=, prompt_id= and covers= beside it"
        )
    if "reviewed" in values:
        bounds = values["reviewed"].split("..")
        if len(bounds) != 2 or not all(UUID_RE.match(bound) for bound in bounds):
            reasons.append(
                f"reviewed={values['reviewed']!r} is malformed; expected "
                "<first prompt uuid>..<last prompt uuid>"
            )
    if "operator_span" in present and not row.value("corrects"):
        reasons.append(
            "operator_span= needs corrects=<row stamp> to say whose quote it spans"
        )
    return reasons


def _quote_length(row: Row) -> int | None:
    """Characters inside the first quoted cell of `row`, or None when it holds none."""
    for cell in row.cells:
        if re.match(r"(?i)^question=", cell):
            continue
        match = QUOTED_RE.search(cell)
        if match:
            return len(match.group(0)) - 2
    return None


def _span_target_refusal(row: Row, rows_by_stamp: dict[str, list[Row]]) -> str | None:
    """The state half of `operator_span`: the span lies inside the target's quote."""
    corrects = row.value("corrects")
    span = row.value("operator_span")
    bounds = _span_bounds(span) if span else None
    if corrects is None or bounds is None:
        return None  # a shape refusal already names it
    match = ID_RE.match(corrects) or STAMP_RE.match(corrects)
    stamp = corrects[:20] if match else corrects
    lane = match.groupdict().get("lane") if match else None
    targets = [
        target
        for target in rows_by_stamp.get(stamp, [])
        if lane is None or target.token("lane") == lane
    ]
    if not targets:
        return (
            f"operator_span={span} cannot be checked: corrects={corrects} is not a row of this "
            "ledger"
        )
    length = _quote_length(targets[0])
    if length is None:
        return f"operator_span={span} cannot be checked: row {corrects} holds no quoted words"
    if bounds[1] > length:
        return (
            f"operator_span={span} lies outside the {length} characters of the quote in row "
            f"{corrects}"
        )
    return None


# --- OMN-17427: a ruling link the ruling audit would discard ---------------
#
# The ruling audit (queue-status ruling_mechanisms.py, OMN-19854) resolves a process
# ruling only through a link row naming a wired mechanism, or a kind=decision demotion
# backed by an operator row. A test file is never a mechanism there, and a demotion
# without operator-row= is listed as DEMOTION-REFUSED. Both shapes used to append
# cleanly, so the lane that wrote them closed believing the ruling resolved, and the
# audit kept counting it (68 of 182 mechanism= CORRECTION rows named a test, and 89 of
# 90 kind=decision CORRECTION rows carried no operator-row=, read 2026-10-17). These
# rules mirror the audit's own, so a row the audit discards is refused when written.
# The limit: operator-row= is checked for shape here; that it names a real RULING or
# OPERATOR-CONSENT row is still the audit's read.
RULING_LINK_KEYS = ("corrects", "amends", "re", "ruling", "rulings", "mechanism-for")
RULING_LINK_ROW_TYPES = frozenset({"CORRECTION", "STATUS", "ACK", "TERMINAL"})
_TEST_NAME_RE = re.compile(r"^test_.*\.(?:py|sh)$|.*_test\.(?:py|sh)$")
_STAMP_IN_RE = re.compile(STAMP)


def _is_test_path(path: str) -> bool:
    """The audit's rule (is_test_file): a test proves a mechanism, it never is one."""
    parts = path.split("/")
    return (
        any(part in ("tests", "test") for part in parts[:-1])
        or parts[-1] == "conftest.py"
        or bool(_TEST_NAME_RE.match(parts[-1]))
    )


def ruling_link_refusals(row: Row | None) -> list[str]:
    """Refusals for a mechanism= or kind=decision cell the ruling audit would discard."""
    if row is None:
        return []
    reasons: list[str] = []
    for value in row.fields.get("mechanism", []):
        for part in (item.strip() for item in value.split(",")):
            if part and not re.search(r"\s", part) and _is_test_path(part):
                reasons.append(
                    f"mechanism={part} is a test file; the ruling audit never counts a test "
                    "(OMN-19854): name the script, hook, timer or workflow it proves"
                )
    linked = any(
        _STAMP_IN_RE.search(value)
        for key in RULING_LINK_KEYS
        for value in row.fields.get(key, [])
    )
    demotes = (
        row.row_type in RULING_LINK_ROW_TYPES
        and linked
        and "decision" in row.fields.get("kind", [])
        and not row.value("mechanism")
        and not row.value("mechanism-ticket")
    )
    if demotes and not any(
        _STAMP_IN_RE.search(v) for v in row.fields.get("operator-row", [])
    ):
        reasons.append(
            f"kind=decision on a {row.row_type} that links a ruling needs "
            "operator-row=<stamp of the RULING or OPERATOR-CONSENT row that makes it a one-off>; "
            "without one the ruling audit refuses the demotion (OMN-19854)"
        )
    return reasons


# A CLAIM's cause=, check= and repo= cells name the owner of a shared red cause, and the landing
# controller reads them as bare values (OMN-17427). 2026-10-30T11:28:16Z: a CLAIM wrote
# cause="omnibase_infra:Hostile Review Thread Gate" read ...; the reader took the quote as part of
# the repository, one bad owner refused the controller's whole decision and the 11:32:03Z tick
# landed nothing. The reader now unquotes and skips what it cannot read; the writer refuses it.
OWNER_CELL_KEYS = ("cause", "check", "repo")
QUOTE_OPENERS = ('"', "'", "\u201c", "\u2018")


def quoted_owner_cell_refusals(row: Row) -> list[str]:
    """One refusal per cause=, check= or repo= cell of a CLAIM whose value opens with a quote."""
    return [
        f"{key}={value[:40]!r} opens with a quote; a CLAIM's {key}= cell takes the bare value "
        "(cause=<repo>:<check>, spaces in the check stay as they are), and free text goes in "
        "its own cell after the others, never inside one (OMN-17427)"
        for key in OWNER_CELL_KEYS
        for value in row.fields.get(key, [])
        if value.startswith(QUOTE_OPENERS)
    ]


def row_refusals(line: str) -> list[str]:
    """Every reason `line` (one row-opening line) breaks the grammar. Empty
    when it conforms."""
    row = parse_row(line)
    if row is None:
        return [
            "a new row is '<YYYY-MM-DDTHH:MM:SSZ> | <TYPE> | key=value | ... | text': "
            "the full UTC timestamp first, then the type cell, with no bullet and no "
            "leading table pipe"
        ]
    reasons: list[str] = []
    check = delegation_cell_reason(line)
    if check:
        reasons.append(check)
    if row.row_type not in CANONICAL_TYPES:
        return [_type_refusal(row.row_type), *reasons]

    spec = GRAMMAR[row.row_type]
    required_raw = spec["required"]
    if not isinstance(required_raw, list | tuple):
        raise TypeError(f"GRAMMAR[{row.row_type!r}]['required'] is not a sequence")
    required = [str(item) for item in required_raw]
    for item in required:
        if item in {"lane", "from"}:
            if row.token(item) is None:
                reasons.append(f"missing {item}= (its own cell, {item}=<lane>)")
        elif (
            item in {"to", "id", "re", "worktree"}
            or item.startswith('"')
            or item.endswith("<list>")
        ):
            continue  # judged by the dedicated checks below
        elif not _has_any(row, item):
            names = " or ".join(f"{key}=" for key in item.split("|"))
            reasons.append(f"missing {names}")

    worktree_values = row.fields.get("worktree", [])
    if (
        row.row_type == "CLAIM"
        and claim_contract.enforced()
        and not claim_contract.exempt_actor(row.fields)
    ):
        check = claim_contract.claim_refusal(row.fields)
        if check:
            reasons.append(f"OMN-20029: {check}")
    if row.row_type == "CLAIM":
        reasons.extend(quoted_owner_cell_refusals(row))
    if row.row_type == "CLAIM" and (
        worktree_values or not worktree_cell.exempt_identity(line)
    ):
        check = worktree_cell.claim_refusal(worktree_values)
        if check:
            reasons.append(check)
    if row.row_type == "TERMINAL":
        check = worktree_cell.terminal_shape_refusal(worktree_values)
        if check:
            reasons.append(check)

    if row.row_type in MESSAGE_TYPES:
        for check in (_to_refusal(row), _id_refusal(row, "from")):
            if check:
                reasons.append(check)
        lane = row.token("lane")
        sender = row.token("from")
        if lane is not None and sender is not None and lane != sender:
            reasons.append(
                f"lane={lane} and from={sender} disagree; the sender is from="
            )
        if row.row_type == "ACK":
            check = _re_refusal(row)
            if check:
                reasons.append(check)
        reasons.extend(_consent_shape_refusals(row))

    if row.row_type == "HOLD":
        check = _id_refusal(row, "lane")
        if check:
            reasons.append(check)
        release_values = row.fields.get("release", [])
        operator_release = release_values == ["operator"]
        if release_values and not operator_release:
            reasons.append(
                f"release={','.join(release_values)!r} is malformed; expected exactly "
                "release=operator"
            )
        if operator_release and row.value("ruling") is None:
            reasons.append("release=operator requires ruling=<row stamp or id>")
        if row.value("to") is not None:
            check = _to_refusal(row)
            if check:
                reasons.append(check)
        if row.value("surface") is not None:
            until = row.token("until")
            if until is None or STAMP_RE.match(until) is None:
                reasons.append(
                    "a HOLD on a surface carries until=<YYYY-MM-DDTHH:MM:SSZ>"
                )
        operator_condition = operator_release and row.value("ruling") is not None
        if (
            not any(row.value(key) for key in ("after", "until", "proof"))
            and not operator_condition
        ):
            reasons.append(
                "a HOLD needs a release condition: after=<repo>#<n>, "
                "until=<YYYY-MM-DDTHH:MM:SSZ>, proof=<row id>, or release=operator with "
                "ruling=<row stamp or id>"
            )
        for key, pattern in (
            ("after", PR_VALUE_RE),
            ("proof", RE_VALUE_RE),
            ("pr", PR_VALUE_RE),
            ("ruling", RE_VALUE_RE),
        ):
            check = _comma_values_refusal(row, key, pattern)
            if check:
                reasons.append(check)
        for value in row.fields.get("until", []):
            if STAMP_RE.fullmatch(value) is None:
                reasons.append(
                    f"until={value!r} is malformed; expected YYYY-MM-DDTHH:MM:SSZ"
                )

    if row.row_type == "RELEASE":
        check = _re_refusal(row)
        if check:
            reasons.append(check)
        if row.value("surface") is None and row.value("pr") is not None:
            re_value = row.token("re")
            if re_value is not None and ID_RE.match(re_value):
                check = _comma_values_refusal(row, "pr", PR_VALUE_RE)
                if check:
                    reasons.append(
                        f"{check}; a RELEASE of a hold id frees only the PRs in pr=, so "
                        "pr= is <repo>#<n>[,<repo>#<n>...] (OMN-20956)"
                    )
        if row.value("surface") is not None:
            if row.token("result") not in SURFACE_RESULTS:
                reasons.append("a surface RELEASE carries result=<PASS|FAIL|ABORTED>")
            if row.token("restored") not in SURFACE_RESTORED:
                reasons.append("a surface RELEASE carries restored=<yes|no>")

    if row.row_type in {"RULING", "OPERATOR-CONSENT"}:
        check = _quoted_refusal(row)
        if check:
            reasons.append(check)
    if row.row_type == "RULING":
        questions = row.fields.get("question", [])
        if (
            len(questions) != 1
            or questions[0].strip('"“”').strip().lower() in PLACEHOLDER_VALUES
        ):
            reasons.append(
                "a RULING needs one non-empty question= cell naming the question or options "
                "the operator's words answer (OMN-20656)"
            )
    if row.row_type == "OPERATOR-CONSENT":
        reasons.extend(_scope_list_refusals(row))
    reasons.extend(_link_refusals(row))
    reasons.extend(ruling_link_refusals(row))
    check = hold_instruction_refusal(line, row.row_type)
    if check:
        reasons.append(check)
    return reasons


def refusal_for_payload(payload: str, ledger_name: str) -> str | None:
    """The refusal for appending `payload` to `ledger_name`, or None.

    Pure: reads nothing but its arguments, so the caller runs it before the
    lock. Only row-opening lines are judged; a continuation line is the body
    of the row above it.
    """
    if not governs(ledger_name):
        return None
    reasons: list[str] = []
    grouped_rows: list[tuple[int, list[str]]] = []
    for number, line in enumerate(payload.splitlines(), start=1):
        if opens_a_row(line):
            grouped_rows.append((number, [line]))
        elif grouped_rows:
            grouped_rows[-1][1].append(line)
    for number, lines in grouped_rows:
        line_reasons = row_refusals(lines[0])
        row = parse_row(lines[0])
        if (
            len(lines) > 1
            and row is not None
            and not any(
                "free text cannot hold a PR" in reason for reason in line_reasons
            )
        ):
            check = hold_instruction_refusal("\n".join(lines), row.row_type)
            if check:
                line_reasons.append(check)
        for reason in line_reasons:
            reasons.append(f"line {number}: {reason}")
    if not reasons:
        return None
    return (
        f"OMN-19256 row grammar rejected append to {ledger_name} -- "
        + "; ".join(reasons)
        + ". Nothing was written. The grammar is printed by "
        "'onex-ledger --print-grammar'."
    )


# --- OMN-19261: one live HOLD per proof surface ----------------------------
#
# Problem (fact). The pre-PR proof-surface lease was decided OUTSIDE the lock:
# the runbook's reader grepped the ledger and took "the last row naming the
# surface", then the HOLD was appended as a second, separate step. Two lanes
# could both read free and both append a HOLD, and a reaped lane's late RELEASE
# of its own hold made the surface read free while the reaper's hold was live.
# The id check above refused neither: two lanes' hold ids always differ.
#
# Rule. A HOLD carrying surface=S is refused while ANOTHER lane has a HOLD on
# S that is live: no RELEASE anywhere on the ledger (or earlier in the same
# payload) names its id with re=, and its until= is later than the new HOLD's
# own timestamp. Keyed on the hold id, never on the surface name or on row
# order, so a RELEASE only ever frees the hold it names. The instant judged is
# the new row's own stamp, which the OMN-17427 clock guard has already pinned
# to the real clock before the lock is taken, so the decision is deterministic
# and replayable from the ledger text alone. A surface HOLD whose until= cannot
# be read is live until a RELEASE names it: an unreadable expiry fails closed.
# A lane's own live hold does not block it (a renewal), and the rows are read
# in order, so a payload may reap an expired or abandoned hold with a RELEASE
# and take the surface with a HOLD in one atomic append.


class _SurfaceHold:
    __slots__ = ("hold_id", "lane", "until")

    def __init__(self, hold_id: str, lane: str, until: str | None) -> None:
        self.hold_id = hold_id
        self.lane = lane
        self.until = until

    def live_at(self, stamp: str) -> bool:
        if self.until is None:
            return True
        # Both are fixed-width UTC stamps, so string order is time order.
        return self.until > stamp


def _surface_of(row: Row) -> str | None:
    value = row.value("surface")
    return value.strip() if value else None


class _SurfaceLeases:
    """The unreleased surface holds on a ledger, folded row by row."""

    def __init__(self) -> None:
        self.holds: dict[str, dict[str, _SurfaceHold]] = {}
        self.released: set[str] = set()

    def fold(self, row: Row) -> None:
        if row.row_type == "RELEASE":
            target = row.token("re")
            if target:
                self.released.add(target)
                for by_id in self.holds.values():
                    by_id.pop(target, None)
            return
        if row.row_type != "HOLD":
            return
        surface = _surface_of(row)
        hold_id = row.token("id")
        lane = row.token("lane")
        if surface is None or hold_id is None or lane is None:
            return
        if hold_id in self.released:
            return
        until = row.token("until")
        if until is not None and STAMP_RE.match(until) is None:
            until = None
        self.holds.setdefault(surface, {})[hold_id] = _SurfaceHold(hold_id, lane, until)

    def blocker(self, row: Row) -> tuple[str, _SurfaceHold] | None:
        """The live hold of another lane that `row`, a surface HOLD, collides
        with, or None."""
        surface = _surface_of(row)
        lane = row.token("lane")
        if surface is None:
            return None
        for hold in self.holds.get(surface, {}).values():
            if hold.lane != lane and hold.live_at(row.stamp):
                return surface, hold
        return None


def _surface_refusal(
    ledger_name: str, number: int, surface: str, hold: _SurfaceHold, stamp: str
) -> str:
    until = hold.until if hold.until is not None else "<unreadable, so live>"
    return (
        f"OMN-19261 surface lease rejected append to {ledger_name} -- line {number}: "
        f"surface={surface} is held by lane={hold.lane} (id={hold.hold_id}, "
        f"until={until}), which no RELEASE re= names and which has not expired at "
        f"this row's timestamp {stamp}. Wait for RELEASE re={hold.hold_id} or for "
        "its until= to pass, then append again (a reap is a RELEASE naming that id, "
        "under the runbook's reaping rule). Nothing was written."
    )


# --- OMN-20148: a TERMINAL accounts for the worktree its CLAIM named -------
#
# The pure half (row_refusals) judges the shape of a CLAIM's and a TERMINAL's
# worktree= cell. This is the state half: which CLAIM the TERMINAL closes, and
# whether the path it says is removed is in fact gone. The rules and their
# limit are in worktree_cell.py.


class _ClaimIndex:
    """The CLAIM rows of a ledger, by lane, in append order."""

    def __init__(self) -> None:
        self.by_lane: dict[str, list[Row]] = {}

    def fold(self, row: Row) -> None:
        if row.row_type != "CLAIM":
            return
        lane = row.token("lane")
        if lane is not None:
            self.by_lane.setdefault(lane, []).append(row)

    def closed_by(self, terminal: Row) -> Row | None:
        """The CLAIM `terminal` closes: the one its closes-CLAIM=/closes= stamp names,
        or None when that stamp is not live. Without a stamp, the newest of its lane,
        preferring the same ticket. None when not found."""
        lane = terminal.token("lane")
        if lane is None:
            return None
        candidates = self.by_lane.get(lane, [])
        if not candidates:
            return None
        named = terminal.value("closes-claim") or terminal.value("closes")
        stamped = re.match(STAMP, named) if named else None
        if stamped is not None:
            for claim in reversed(candidates):
                if claim.stamp == stamped.group(0):
                    return claim
            # OMN-20864: a named CLAIM may have rolled; another live CLAIM imposes nothing.
            return None
        ticket = terminal.token("ticket")
        if ticket:
            for claim in reversed(candidates):
                if ticket in {
                    claim.token("ticket"),
                    *(claim.value("tickets") or "").split(","),
                }:
                    return claim
        return candidates[-1]


def _terminal_worktree_refusal(terminal: Row, claims: _ClaimIndex) -> str | None:
    closed = claims.closed_by(terminal)
    claimed_value = closed.value("worktree") if closed is not None else None
    claimed = claimed_value if worktree_cell.names_a_worktree(claimed_value) else None
    values = terminal.fields.get("worktree", [])
    if not values:
        if claimed is not None and not worktree_cell.exempt_identity(terminal.text):
            return worktree_cell.required_terminal_refusal(claimed)
        return None
    disposition, why = worktree_cell.parse_disposition(values[0])
    if disposition is None:
        return why
    return worktree_cell.disposition_refusal(disposition, claimed)


# The payload row types the state half judges against the ledger: an id is declared once (MSG,
# ACK, HOLD), a HOLD's surface lease, and the CLAIM a TERMINAL closes.
ID_ROW_TYPES = frozenset({"MSG", "ACK", "HOLD"})
STATE_ROW_TYPES = ID_ROW_TYPES | {"TERMINAL"}


def refusal_for_state(payload: str, existing: str, ledger_name: str) -> str | None:
    """The state-dependent half: an id may be declared once (OMN-19256), and a
    proof surface has at most one live HOLD (OMN-19261). `existing` is the
    ledger text, read by the caller inside the held lock, so the check and the
    write that follows it cannot be separated by another lane's append."""
    if not governs(ledger_name):
        return None
    rows = [row for row in map(parse_row, payload.splitlines()) if row is not None]
    spans = [
        row
        for row in rows
        if row.row_type == "CORRECTION" and "operator_span" in row.fields
    ]
    if spans:
        by_stamp: dict[str, list[Row]] = {}
        for line in existing.splitlines():
            prior = parse_row(line)
            if prior is not None:
                by_stamp.setdefault(prior.stamp, []).append(prior)
        for number, candidate in enumerate(rows, start=1):
            if candidate not in spans:
                continue
            reason = _span_target_refusal(candidate, by_stamp)
            if reason is not None:
                return (
                    f"OMN-18531 row grammar rejected append to {ledger_name} -- row {number}: "
                    f"{reason}. Nothing was written."
                )
    if not any(row.row_type in STATE_ROW_TYPES for row in rows):
        # OMN-17427: a payload of STATUS, CLAIM or FRICTION rows declares no id, takes no surface
        # and closes no CLAIM. Parsing every row of a 40 MB ledger here, inside the held lock,
        # was most of each append's time (0.54 s of 0.86 s measured on a copy of the ledger).
        return None
    seen: set[str] = set()
    leases = _SurfaceLeases()
    claims = _ClaimIndex()
    for line in existing.splitlines():
        row = parse_row(line)
        if row is not None:
            if row.row_type in ID_ROW_TYPES:
                value = row.token("id")
                if value:
                    seen.add(value)
            leases.fold(row)
            claims.fold(row)
    for number, line in enumerate(payload.splitlines(), start=1):
        row = parse_row(line)
        if row is None:
            continue
        if row.row_type == "TERMINAL":
            reason = _terminal_worktree_refusal(row, claims)
            if reason is not None:
                return (
                    f"OMN-20148 worktree cell rejected append to {ledger_name} -- "
                    f"line {number}: {reason}. Nothing was written."
                )
        claims.fold(row)
        if row.row_type in ID_ROW_TYPES:
            value = row.token("id")
            if value and value in seen:
                return (
                    f"OMN-19256 row grammar rejected append to {ledger_name} -- line {number}: "
                    f"id={value} is already declared on this ledger; an id names one row. "
                    "Nothing was written."
                )
            if value:
                seen.add(value)
        if row.row_type == "HOLD":
            collision = leases.blocker(row)
            if collision is not None:
                surface, hold = collision
                return _surface_refusal(ledger_name, number, surface, hold, row.stamp)
        leases.fold(row)
    return None


# --- OMN-20891: one key per operator question --------------------------------
#
# RULING 2026-10-10T16:48:52Z lane=park-default-9f8a: no automated sender asks
# the operator the same thing twice. Every sender (the landing controller's park
# MSG, a lane's `row MSG --to operator`, the session ticks, queue-status NEXT)
# derives its question's key here and nowhere else, so two senders asking about
# one subject collide on one key. This module is the seam: the append path
# imports it, and the plugin scripts load it by path, so it stays standard library only.
#
# The key is the subject, never the episode. The landing controller's own
# dedupe cell was `<cause>@<parked_until>`, so each new park of the same cause
# was a new key and a new question: ten park MSGs on 2026-10-09/10 for eight
# causes. The episode suffix and any stamp are dropped here.
QUESTION_KEY_FIELDS = ("qkey", "cause", "subject", "question")
_EPISODE_SUFFIX_RE = re.compile(
    r"@\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE
)
_ANY_STAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_SUBJECT_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/#+-]*$")
SUBJECT_TOKEN_MAX = 120


def operator_question_key(subject: str) -> str:
    """The one key of an operator question about `subject` (OMN-20891).

    A subject that is one token (`cause:OmniNode-ai/omnibase_infra:f891e934ce4e`,
    `repo#782`) is its own key, case-folded and without an `@<stamp>`
    episode suffix. Any other text is case-folded, its stamps and quotes
    removed and its whitespace collapsed, and keyed as `text:<16 hex>` of that.
    Idempotent: the key of a key is the key."""
    text = " ".join(subject.strip().strip("\"'`").casefold().split())
    text = _EPISODE_SUFFIX_RE.sub("", text)
    if not text:
        raise ValueError("an operator question needs a subject to key on")
    if len(text) <= SUBJECT_TOKEN_MAX and _SUBJECT_TOKEN_RE.match(text):
        return text
    folded = " ".join(_ANY_STAMP_RE.sub(" ", text).replace('"', " ").split())
    return "text:" + hashlib.sha256(folded.encode("utf-8")).hexdigest()[:16]


def is_operator_question(row: Row) -> bool:
    """A MSG addressed to the operator: everything such a row says is operator-facing."""
    if row.row_type != "MSG":
        return False
    return any(
        part.strip().casefold() == "operator"
        for value in row.fields.get("to", [])
        for part in value.split(",")
    )


def question_key_for_row(row: Row) -> str:
    """The key of an operator question row: its `qkey=`, else its `cause=`, else its
    `subject=`, else its `question=` text, else its free text, through
    operator_question_key."""
    for key in QUESTION_KEY_FIELDS:
        value = row.value(key)
        if value:
            return operator_question_key(value)
    free = [cell for cell in row.cells if not FIELD_RE.match(cell)]
    return operator_question_key(" ".join(free) or row.text)


def grammar_json() -> str:
    """The grammar as JSON: the single source skills and readers cite."""
    document = {
        "version": GRAMMAR_VERSION,
        "governed_ledgers": sorted(GOVERNED_LEDGER_NAMES),
        "row_shape": "<YYYY-MM-DDTHH:MM:SSZ> | <TYPE> | key=value | ... | free text",
        "field_rule": (
            "a field is a pipe cell that starts with key=; lane, from, to, id and re "
            "are a whole cell holding one token"
        ),
        "id_rule": (
            "id=<the row's own timestamp>-<sender lane>; ids and re= never carry a line number"
        ),
        "types": GRAMMAR,
        "legacy_type_map": LEGACY_TYPE_MAP,
        "tool_internal_types": sorted(TOOL_INTERNAL_TYPES),
        "message_consent_markers": list(CONSENT_MARKERS)
        + sorted(f"{k}=" for k in CONSENT_FIELD_KEYS),
        "scope_placeholders_refused": sorted(PLACEHOLDER_VALUES - {""}),
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
