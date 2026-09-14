# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The criterion identity a companion contract PINS, in the consumer's own terms.

OMN-18332. A binding's ``criterion_hash`` is not an internal number. It is the
one field in the record a CONSUMER recomputes: `onex_change_control`'s
``ac_binding_stale_hash`` rule reads the live ticket body, hashes the criterion
it finds, and refuses the binding when the two differ. So the hash this producer
pins has to be the hash that consumer computes, over the text that consumer
reads -- or every binding this producer mints is refused as stale on arrival.

It was. Measured on OCC#9486, the companion minted for OMN-18358 on
2026-09-14T08:02Z: all six pinned hashes disagreed with the consumer's, total
mismatches rather than near misses. The cause was not a broken hash function.
:func:`omnimarket.occ_criterion_units.criterion_units` and the consumer's
``criterion_hash`` agree byte-for-byte on the same input; the transcriber was
handing them DIFFERENT input. It pinned the hash of the
:func:`omnimarket.occ_criterion_normalizer.markdown_comparison_text` projection
-- inline markup discarded -- because that projection is the only form in which
the live markdown and the creation revision's rich text can be compared at all.
The consumer hashes the raw markdown. ``**AC1** -- ... `git checkout` ...``
against ``AC1 -- ... git checkout ...`` is two different sentences to sha256.

**So the projection keeps its job and loses this one.** It still decides whether
a criterion's text has MOVED since creation, which is the question it exists to
answer and the only question it can answer. What goes in the contract is the
consumer-space pin computed here.

**This module is a PORT, not a second hash.** The authority is
`onex_change_control` ``src/onex_change_control/validation/ac_criteria.py`` at
:data:`PORTED_FROM_REVISION` -- every span below is that file's bytes. It is
ported rather than imported because `onex_change_control` is not in this
repository's dependency set at all, in any group, and is not installed in the
``omninode-runtime-effects`` image the transcriber runs in, so ``import
onex_change_control`` inside the one code path that must never fail open would
raise ``ModuleNotFoundError``. The same coupling already runs in the other
direction and is stated on that side: `omnibase_infra`'s evidence closer carries
its own port of ``normalise_criterion`` / ``criterion_hash`` for the same
reason, and OCC's reader is itself a verbatim port of that closer's.

**The drift control.** ``tests/unit/occ/test_omn_18332_contract_pin_drift.py``
holds the pinned upstream text as a committed fixture and asserts this module
still contains every span byte-for-byte; a second leg, which runs wherever an
`onex_change_control` clone is reachable, re-derives the fixture from that
clone and recomputes a corpus of digests through the REAL consumer module,
asserting they equal this one's. The honest bound is the same one the sibling
vendor test records: hosted CI has the clone-free leg only, which proves this
module matches the snapshot, not that the snapshot still matches upstream.

**OMN-18356 re-extraction.** The consumer widened its label grammar to accept
a suffixed criterion (``AC2b``, ``AC10a``): an optional single-letter suffix
group, captured verbatim (case preserved), directly adjacent to the ordinal
digits. `_AC_LABEL_RE` and `canonical_ac_label` are the only two spans that
changed; every other ported definition is byte-identical to the prior pin.
This closes the exact gap OMN-18332's own contract exposed: six of its twelve
declared criteria carry a suffixed label, and the un-widened port could never
resolve one to a key `contract_pin_hashes` would emit, regardless of what the
consumer itself now accepts.

**What this module deliberately does NOT do.** It does not read falsifiers and
it does not decide acceptance. It answers exactly one question -- what digest
will the gate compute for this label -- and a label it cannot resolve is
reported as absent rather than guessed, so the transcriber withholds a binding
the consumer could never match instead of minting one it will refuse.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: The `onex_change_control` commit the spans below were taken from.
PORTED_FROM_REVISION: Final[str] = "ab4be01bda1f2f1e50859b3097014ded955ab1b7"

#: The upstream file they came from, relative to the `onex_change_control` root.
PORTED_FROM_PATH: Final[str] = "src/onex_change_control/validation/ac_criteria.py"

__all__ = [
    "MAX_CRITERION_HASH_INPUT_CHARS",
    "PORTED_FROM_PATH",
    "PORTED_FROM_REVISION",
    "acceptance_criteria_items",
    "canonical_ac_label",
    "contract_pin_hashes",
    "criteria_by_label",
    "criterion_hash",
    "is_ac_heading",
    "item_text",
    "normalise_criterion",
]


# ---------------------------------------------------------------------------
# PORTED SPANS BEGIN -- byte-identical to the upstream revision named above.
# Do not edit by hand; re-extract from upstream and bump the pin instead.
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*)$")
_AC_ITEM_RE = re.compile(
    r"^[ \t]*([*_]*)[ \t]*(AC[-_ ]?\d+)(?!\d)[*_]*(.*?)[ \t]*$", re.IGNORECASE
)
_TRAILING_EMPHASIS_RE = re.compile(r"[*_]+$")
_TRAILING_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")
_TASK_MARKER_RE = re.compile(r"^\[[ \t xX]\][ \t]*")
_HEADING_ENUM_RE = re.compile(r"^\d+[.)]\s*")
#: OMN-18356: the optional single-letter SUFFIX group matches the producer's
#: grammar (``omniclaude`` ``_CRITERION_LABEL``) exactly -- a round split into
#: ``AC2b``/``AC2c``/... sits a letter directly after the ordinal digits, with
#: no boundary between them (both are word characters), so a bare ``(\d+)\b``
#: never matched past the digits and the whole label was lost. The suffix is
#: captured, not discarded, and read verbatim (case preserved) in
#: :func:`canonical_ac_label`: ``AC2b`` and ``AC2B`` are different labels, not
#: the same criterion written twice. A plain ``AC2`` is unaffected -- the
#: suffix group matches zero characters and the boundary check falls back to
#: its original position.
_AC_LABEL_RE = re.compile(
    r"^[\s>*_+-]*(?:\*\*)?\s*(AC|DOD)[-_ .]?(\d+)([a-zA-Z]?)\b", re.IGNORECASE
)

_AC_HEADING_TEXTS = frozenset(
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
)
#: Only multi-word spellings are eligible for the leading-qualifier match, so a
#: heading that merely ENDS in "ac" or "dod" ("## Notes on AC") does not open a
#: criteria section over unrelated content.
_AC_HEADING_PHRASES = frozenset(text for text in _AC_HEADING_TEXTS if " " in text)

_WHITESPACE_RUN_RE = re.compile(r"\s+")

#: Ceiling on the text fed to the hash. A criterion longer than this is
#: truncated before hashing, deterministically, so a pathological body cannot
#: make the hash depend on how much of it somebody pasted. Generous enough that
#: no real criterion reaches it.
MAX_CRITERION_HASH_INPUT_CHARS = 4000


def is_ac_heading(line: str) -> bool:
    """True when ``line`` reads as an acceptance-criteria heading.

    Tolerates ``## Acceptance Criteria``, ``**Acceptance criteria:**``,
    ``### 3. Acceptance criteria``, a trailing qualifier
    (``Acceptance criteria (falsifiable)``) and a leading one
    (``Falsifiable acceptance criteria``).
    """
    raw = line.strip()
    if not raw:
        return False
    looks_like_heading = raw.startswith("#") or (
        raw.startswith("**") and raw.endswith("**")
    )
    text = raw.lstrip("#").strip()
    text = text.strip("*_").strip()
    text = _HEADING_ENUM_RE.sub("", text)
    text = text.rstrip(":").strip()
    text = text.strip("*_").strip()
    folded = text.casefold()
    if folded in _AC_HEADING_TEXTS:
        return True
    trimmed = _TRAILING_QUALIFIER_RE.sub("", folded).strip().rstrip(":").strip()
    if trimmed in _AC_HEADING_TEXTS:
        return True
    return looks_like_heading and any(
        trimmed.endswith(f" {known}") for known in _AC_HEADING_PHRASES
    )


def item_text(line: str) -> str:
    """The criterion text ``line`` carries, or ``""`` when it carries none.

    One function so a criterion read inside a section and the same criterion
    read by the whole-body fallback below produce the IDENTICAL string. Two
    extractors would give one criterion two hashes, and a binding would go
    stale for no reason but which pass happened to find it.
    """
    list_match = _LIST_ITEM_RE.match(line)
    if list_match:
        return _TASK_MARKER_RE.sub("", list_match.group(1)).strip()
    ac_match = _AC_ITEM_RE.match(line)
    if not ac_match:
        return ""
    lead, token, rest = ac_match.groups()
    text = f"{token}{rest}".strip()
    if lead:
        text = _TRAILING_EMPHASIS_RE.sub("", text).strip()
    return text


def acceptance_criteria_items(description: str) -> list[str]:
    """Items listed under an acceptance-criteria heading in ``description``.

    A section runs from an acceptance-criteria heading to the next markdown
    heading. When the body carries NO recognised heading at all, the whole body
    is the section — the closer's own fallback, kept because a ticket that lists
    its criteria under a prose opener otherwise parses as having none.

    ONE DELIBERATE DIVERGENCE FROM THE CLOSER, measured 2026-09-12.
    -------------------------------------------------------------
    The closer BREAKS out of the scan at the first non-criteria heading, so it
    reads the FIRST criteria section and nothing after it. Measured against the
    67 live contracts that declare a binding: OMN-18186 carries AC1 to AC4 under
    ``## Acceptance criteria`` and AC5, AC6 under a later
    ``### Added acceptance criteria``, and the closer reads four. Its contract
    binds all six, so two real, author-written criteria read as fabricated.

    This reader CLOSES the section at a non-criteria heading and RE-OPENS at the
    next criteria heading, so every criteria section in the body is read. That
    direction is safe by the closer's own rule — over-reading holds a flip,
    under-reading releases one — and it is the difference between a gate that
    says "your ticket does not have AC5" and one that is right.

    The closer needs the same one-line change and does not have it yet; until it
    does, it under-reads exactly these tickets. That gap is reported rather than
    patched from here: this repository cannot edit that one, and a silent
    divergence would be worse than a stated one.
    """
    items: list[str] = []
    saw_ac_heading = any(is_ac_heading(line) for line in description.splitlines())
    in_section = not saw_ac_heading
    for line in description.splitlines():
        if is_ac_heading(line):
            in_section = True
            continue
        if not in_section:
            continue
        if line.lstrip().startswith("#") and saw_ac_heading:
            in_section = False
            continue
        text = item_text(line)
        if text:
            items.append(text)
    return items


def canonical_ac_label(text: str) -> str:
    """``AC3`` / ``DOD2`` parsed from a criterion or a binding entry, or ``""``.

    Both sides of the join go through this one function, so a contract writing
    ``ac-3`` and a ticket writing ``**AC3**`` bind, and neither side can
    normalise differently from the other.
    """
    match = _AC_LABEL_RE.match(text.strip())
    if not match:
        return ""
    return f"{match.group(1).upper()}{int(match.group(2))}{match.group(3)}"


def normalise_criterion(text: str) -> str:
    """The criterion text the hash is taken over.

    Whitespace runs collapse to one space and the ends are stripped, so
    re-wrapping a paragraph or re-indenting a bullet is not a rewrite. NOTHING
    ELSE is normalised — not case, not punctuation, not markdown emphasis —
    because each of those can change what a criterion requires.
    """
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()[:MAX_CRITERION_HASH_INPUT_CHARS]


def criterion_hash(text: str) -> str:
    """The sha256 hex digest identifying this criterion's current revision."""
    return hashlib.sha256(normalise_criterion(text).encode("utf-8")).hexdigest()


def criteria_by_label(description: str) -> dict[str, str]:
    """``{label: criterion text}`` for every LABELLED criterion in ``description``.

    An unlabelled criterion is omitted rather than given a positional name: an
    ordinal derived from parse position renumbers every binding below it the
    moment a bullet is inserted, which would make every pin stale for a reason
    that has nothing to do with the criterion's text. A ticket whose criteria
    are unlabelled cannot be bound, and the gate says so in those words.

    First occurrence of a label wins. A body that labels two criteria ``AC1``
    is malformed on the ticket side; picking the first is stable, and the gate
    reports the duplicate rather than silently choosing.

    THE WHOLE-BODY FALLBACK, and why it is over-inclusive on purpose.
    ----------------------------------------------------------------
    Criteria sections are read first. Then any LABELLED criterion line the
    sections missed is picked up from anywhere in the body. Measured on the live
    corpus: OMN-17907 writes AC4 and AC5 under ``## Second finding, folded in``
    — a heading no reader will ever recognise as a criteria heading — and the
    ticket body itself says in as many words that they are full acceptance
    criteria rather than commentary. Section-only reading calls that contract's
    AC4 binding a claim on a criterion the ticket does not have, which is false.

    Over-inclusive is the right direction HERE specifically, and only here. This
    map answers two questions: does a claimed label EXIST, and what is its text.
    Being generous about WHERE the author wrote a criterion cannot let a
    fabricated label through — ``AC7`` still appears nowhere — it only stops the
    gate accusing an author of inventing a criterion they actually wrote.
    """
    resolved: dict[str, str] = {}
    for item in acceptance_criteria_items(description):
        label = canonical_ac_label(item)
        if not label or label in resolved:
            continue
        resolved[label] = item

    for raw in description.splitlines():
        text = item_text(raw)
        label = canonical_ac_label(text) if text else ""
        if not label or label in resolved:
            continue
        resolved[label] = text
    return resolved


# ---------------------------------------------------------------------------
# PORTED SPANS END
# ---------------------------------------------------------------------------


def contract_pin_hashes(description: str) -> dict[str, str]:
    """``{label: criterion_hash}`` exactly as the OCC gate will recompute them.

    One call, so a caller cannot accidentally hash a projection, a falsifier or
    a re-flowed copy. A label absent from this map is one the consumer's reader
    cannot resolve in this body, and the transcriber withholds its binding
    rather than pinning a digest that has nothing to match against.
    """
    return {
        label: criterion_hash(text)
        for label, text in criteria_by_label(description).items()
    }
