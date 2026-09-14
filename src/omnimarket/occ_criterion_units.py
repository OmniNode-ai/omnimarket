# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One acceptance criterion, parsed and hashed exactly as the admission guard does.

OMN-18332 (step 6). The transcriber turns a ticket's declared criterion-to-
falsifier map into companion-contract bindings. For a binding's
``criterion_hash`` to mean anything, the hash the transcriber pins must be the
hash the ADMISSION GUARD computed when it admitted the create -- otherwise
"this criterion was rewritten" is answered by two parsers that disagree about
where one criterion ends, and a criterion binds to a check declared for its
neighbour.

**This module is VENDORED, not re-implemented.** Every regex, every helper and
both public entry points below are the bytes of
``plugins/onex/hooks/lib/ticket_creation_guard.py`` in ``OmniNode-ai/omniclaude``
at :data:`VENDORED_FROM_REVISION` (OMN-18331, omniclaude#2153), extracted
mechanically rather than retyped.

**Why vendored rather than imported.** ``omniclaude`` is a Claude Code plugin
repository. It is not a published distribution, it is not in this repo's
dependency set, and it is not installed in the ``omninode-runtime-effects``
image the autobinder runs in -- so ``import omniclaude`` at transcription time
would raise ``ModuleNotFoundError`` inside the one code path that must never
fail open. The brief's other option, a real import, is not available at this
layer; this is the option that is.

**The drift control.** ``tests/unit/occ/test_omn_18332_criterion_units_vendor_drift.py``
holds the pinned upstream text as a committed fixture and asserts this module
still contains every span byte-for-byte. A second leg, which runs only where an
``omniclaude`` clone is present, re-derives that fixture from the pinned
revision and asserts it is unchanged -- so a silent upstream edit is caught on
the lab host and on any developer machine. The honest bound is stated there:
hosted CI has the clone-free leg only, which proves this module matches the
snapshot, not that the snapshot still matches upstream.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

#: The omniclaude commit these bytes were taken from (OMN-18331, omniclaude#2153).
VENDORED_FROM_REVISION: Final[str] = "3b1c66e787d3f45634ead74f8f10da7d8d02d54d"

#: The upstream file the spans below came from, relative to the omniclaude root.
VENDORED_FROM_PATH: Final[str] = "plugins/onex/hooks/lib/ticket_creation_guard.py"


@dataclass(frozen=True, slots=True)
class CriterionPolicy:
    """The two vocabulary fields the vendored parse reads off a ``Policy``.

    The upstream helpers take the guard's whole ``Policy``; only these two
    fields are ever touched, so this is the narrowest shape that satisfies
    them. Structural typing is deliberate -- an upstream ``Policy`` instance
    also satisfies every use below, so a future import would be a drop-in.
    """

    acceptance_criteria_headings: frozenset[str]
    falsifier_markers: tuple[str, ...]


#: Vendored from ``plugins/onex/hooks/config/ticket_creation_policy.json`` at the
#: same revision. Pinned by the same drift test as the code spans: a marker added
#: upstream that is missing here silently stops reading a falsifier the guard
#: admitted, which would demote a properly declared criterion to a draft.
DEFAULT_CRITERION_POLICY: Final[CriterionPolicy] = CriterionPolicy(
    acceptance_criteria_headings=frozenset(
        {
            "acceptance criteria",
            "acceptance criteria (ac)",
            "acceptance criterion",
            "acceptance",
            "ac",
            "acs",
            "definition of done",
            "definition of done (dod)",
            "dod",
        }
    ),
    falsifier_markers=("falsifier:", "falsified by"),
)


# ---------------------------------------------------------------------------
# VENDORED SPANS BEGIN -- byte-identical to the upstream revision named above.
# Do not edit by hand; re-extract from upstream and bump the pin instead.
# ---------------------------------------------------------------------------

_LIST_ITEM: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*)$")

_AC_ITEM: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*([*_]*)[ \t]*(AC[-_ ]?\d+)(?!\d)[*_]*(.*?)[ \t]*$", re.IGNORECASE
)

_TRAILING_EMPHASIS: Final[re.Pattern[str]] = re.compile(r"[*_]+$")

_TASK_MARKER: Final[re.Pattern[str]] = re.compile(r"^\[[ \t xX]\][ \t]*")

_TRAILING_QUALIFIER: Final[re.Pattern[str]] = re.compile(r"\s*\([^)]*\)\s*$")

_HEADING_ENUM: Final[re.Pattern[str]] = re.compile(r"^\d+[.)]\s*")

#: How much of a criterion is quoted back in a refusal. A description whose
#: criteria are paragraphs must not turn one refusal into an unreadable wall,
#: and an unbounded splice is how a message hits a transport limit.

#: The label a downstream binding entry can point AT. Matched against the item
#: text this module returns, which has already had its bullet and any task
#: marker stripped -- so ``**AC1** ...``, ``AC-2: ...``, ``DoD3 -- ...`` and
#: ``ac 4)`` all reach here with the label leading. A criterion with no label
#: is NOT a parse failure: it is an UNBINDABLE criterion, because a binding
#: needs something stable to point at and an ordinal derived from parse
#: position renumbers every binding below it the moment a bullet is inserted.
#: Rule 6 does not refuse it -- that is a ticket-authoring problem reported
#: downstream, where it actually bites.
#:
#: OMN-18356: the optional single-letter SUFFIX group is the fix. A round
#: split into ``AC2b``/``AC2c``/... sits a letter directly after the ordinal
#: digits, with no boundary between them (both are word characters), so a
#: bare ``(\d+)\b`` never matched past the digits and the whole label was
#: lost -- the criterion came back indistinguishable from one with no ordinal
#: at all, and downstream never bound it. The suffix
#: is captured, not discarded, and is read verbatim (case preserved) because
#: it is part of the stable label a binding points at: ``AC2b`` and ``AC2B``
#: are different labels, not the same criterion written twice. A plain
#: ``AC2`` is unaffected -- the suffix group matches zero characters and the
#: boundary check falls back to its original position.
_CRITERION_LABEL: Final[re.Pattern[str]] = re.compile(
    r"^[\s>*_+-]*(?:\*\*)?\s*(AC|DOD)[-_ .]?(\d+)([a-zA-Z]?)\b", re.IGNORECASE
)


def _is_criteria_heading(line: str, policy: CriterionPolicy) -> bool:
    """True when ``line`` opens an acceptance-criteria section.

    Tolerates ``## Acceptance Criteria``, ``**Acceptance criteria:**``,
    ``### 3. Acceptance criteria`` and a bare ``AC``, and strips a trailing
    parenthetical qualifier -- ``Acceptance criteria (falsifiable)`` names the
    section as surely as the bare spelling does. Membership is against the
    configured closed set, never a prefix: a heading reading "Acceptance
    criteria coverage report" is about the section, not the section itself.
    """
    trimmed = line.strip()
    if not trimmed:
        return False
    trimmed = trimmed.lstrip("#").strip()
    trimmed = trimmed.strip("*_").strip()
    trimmed = _HEADING_ENUM.sub("", trimmed).strip()
    trimmed = trimmed.rstrip(":").strip()
    folded = trimmed.lower()
    if folded in policy.acceptance_criteria_headings:
        return True
    return (
        _TRAILING_QUALIFIER.sub("", folded).strip()
        in policy.acceptance_criteria_headings
    )


def _acceptance_criteria_items(description: str, policy: CriterionPolicy) -> list[str]:
    """The criterion items listed under an acceptance-criteria heading.

    The section runs from the heading to the next markdown heading, or to the
    end of the body. An item spans its own line plus any continuation lines
    that follow it before the next item -- so a criterion whose falsifier is
    written on a wrapped line still carries it, and a falsifier belonging to
    the criterion above never discharges the one below.

    Returns an EMPTY list when no recognised heading is present. That differs
    from the closer's parser, which reads the whole body in that case; the
    divergence and its reason are in this module's docstring and in the policy
    file's own comment. Diverging silently would be the defect.
    """
    if not any(_is_criteria_heading(line, policy) for line in description.splitlines()):
        return []

    items: list[list[str]] = []
    in_section = False
    open_item = False
    for line in description.splitlines():
        if _is_criteria_heading(line, policy):
            in_section = True
            open_item = False
            continue
        if not in_section:
            continue
        if line.lstrip().startswith("#"):
            break
        text: str | None = None
        list_match = _LIST_ITEM.match(line)
        if list_match:
            text = _TASK_MARKER.sub("", list_match.group(1)).strip()
        else:
            ac_match = _AC_ITEM.match(line)
            if ac_match:
                lead, token, rest = ac_match.groups()
                text = f"{token}{rest}".strip()
                if lead:
                    text = _TRAILING_EMPHASIS.sub("", text).strip()
        if text is not None:
            if text:
                items.append([text])
                open_item = True
            else:
                open_item = False
            continue
        if not line.strip():
            continue
        if open_item:
            items[-1].append(line.strip())
    return [" ".join(parts).strip() for parts in items if " ".join(parts).strip()]


def _falsifier_of(item: str, policy: CriterionPolicy) -> str | None:
    """The text a criterion names as its falsifier, or ``None``.

    Matched inside the item rather than on a line of its own -- the one place
    this module departs from whole-line anchoring, for the reason the policy
    file records: the falsifier has to be part of the criterion, written in the
    same act, and the item boundary supplies the anchoring instead. The LAST
    marker wins, so a criterion whose prose happens to use the word before
    naming the real one is read the way its author meant it.
    """
    best: str | None = None
    folded = item.lower()
    for marker in policy.falsifier_markers:
        start = folded.rfind(marker)
        if start == -1:
            continue
        tail = item[start + len(marker) :].strip(" \t:-*_")
        if tail and (best is None or start > folded.rfind(best.lower())):
            best = tail
    return best


def canonical_criterion_text(item: str) -> str:
    """The form a criterion is hashed in.

    Unicode NFC, whitespace runs collapsed to single spaces, ends stripped --
    and nothing else. See the module docstring for why the normalisation is
    this narrow.
    """
    return " ".join(unicodedata.normalize("NFC", item).split())


@dataclass(frozen=True, slots=True)
class CriterionUnit:
    """One acceptance criterion, as a self-contained hashable unit.

    ``label`` is ``None`` for a criterion carrying no ``AC<n>``/``DoD<n>``
    ordinal -- unbindable downstream, but not refused here.

    ``falsifier`` is ``None`` for a criterion naming no check, which is exactly
    the population rule 6 refuses, so a consumer never re-derives it.

    ``criterion_hash`` is the SHA-256 of ``text`` encoded UTF-8, hex.
    """

    label: str | None
    text: str
    falsifier: str | None
    criterion_hash: str


def criterion_units(description: str, policy: CriterionPolicy) -> list[CriterionUnit]:
    """Every acceptance criterion in ``description``, one hashable unit each.

    Exported for the binding transcriber. Returns an empty list for a
    description with no recognised criteria heading -- the same scope rule 6
    has.
    """
    units: list[CriterionUnit] = []
    for item in _acceptance_criteria_items(description, policy):
        text = canonical_criterion_text(item)
        match = _CRITERION_LABEL.match(text)
        units.append(
            CriterionUnit(
                label=(
                    f"{match.group(1).upper()}{match.group(2)}{match.group(3)}"
                    if match
                    else None
                ),
                text=text,
                falsifier=_falsifier_of(item, policy),
                criterion_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return units


# ---------------------------------------------------------------------------
# VENDORED SPANS END
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_CRITERION_POLICY",
    "VENDORED_FROM_PATH",
    "VENDORED_FROM_REVISION",
    "CriterionPolicy",
    "CriterionUnit",
    "canonical_criterion_text",
    "criterion_units",
]
