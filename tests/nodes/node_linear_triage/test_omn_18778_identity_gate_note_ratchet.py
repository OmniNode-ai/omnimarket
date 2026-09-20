# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18778: the identity-gate note may not describe a canceled gate as pending.

WHY THIS TEST EXISTS. A comment in ``handler_linear_triage.py`` described a
git-identity independence signal as already implemented, already measured, and
merely switched off pending a prerequisite ticket. Every part of that was false
the day after it was written: the implementing PR was closed UNMERGED on
2026-07-21T09:13:01Z, its ticket was Canceled eighteen seconds later on the
merits, and the prerequisite was Done three days after that and re-scoped
explicitly off independence.

Nothing read the ticket state, so the comment stood for two months and was
believed. On 2026-09-18 an inventory pass read it, concluded the control was
built and shippable, and minted an Urgent ticket to turn on something that does
not exist. A build lane then spent its first hour disproving the premise.

The correction is a comment, and a comment is not a mechanism (CLAUDE.md rule
5). This file is the mechanism. It is deliberately cheap and purely textual: it
owns the claim, not the behaviour.

WHY THE FORBIDDEN STRINGS LIVE HERE AND NOT THERE. The corrected comment
paraphrases the old wording rather than quoting it, precisely so a substring
ratchet can own the file without the file defeating its own test — the same
discipline CLAUDE.md rule 15 applies to PR prose that a gate parses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
_HANDLER = (
    _SRC_ROOT / "nodes" / "node_linear_triage" / "handlers" / "handler_linear_triage.py"
)

# The flag the canceled implementation would have introduced. It never reached
# any branch of this repo; the corrected comment names it only to say so.
_GATE_FLAG = "OMNI_LINEAR_TRIAGE_AUTHOR_IDENTITY_GATE"

# Phrasings that assert the gate is real, or is waiting rather than refused.
# Matched case-insensitively against the handler source.
_FORBIDDEN_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        r"stays?\s+OFF\s+pending",
        "describes the identity gate as switched off awaiting a prerequisite; "
        "it was canceled on the merits, not deferred",
    ),
    (
        r"already\s+built\s+and\s+shadow-measured",
        "describes the identity gate as built; omnimarket#1851 was closed "
        "UNMERGED and nothing under that flag is on any branch",
    ),
    (
        r"is\s+already\s+built",
        "describes the identity gate as built; it is not",
    ),
    (
        r"pending\s+OMN-14893",
        "names OMN-14893 as a live prerequisite; it was Done 2026-07-24 and "
        "re-scoped explicitly off independence",
    ),
)


@pytest.mark.unit
def test_handler_records_the_cancellation() -> None:
    """The note must state the ruling, not merely omit the false claim.

    Silence would let the next reader re-derive the gate from the surrounding
    measurement paragraph, which is what happened the first time.
    """
    source = _HANDLER.read_text(encoding="utf-8")
    assert "OMN-18778" in source, "the correction marker is gone"
    assert "CANCELED" in source or "Canceled" in source, (
        "the note no longer records that OMN-14890 was canceled"
    )
    assert "OMN-14890" in source, "the canceled ticket is no longer named"


@pytest.mark.unit
@pytest.mark.parametrize(("pattern", "why"), _FORBIDDEN_CLAIMS)
def test_handler_does_not_revive_the_identity_gate_claim(
    pattern: str, why: str
) -> None:
    """No phrasing may present the canceled identity gate as real or pending."""
    source = _HANDLER.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.IGNORECASE)
    assert match is None, (
        (
            f"{_HANDLER.name} matches {pattern!r} at offset {match.start()}: {why} "
            "(OMN-18778). Route an evidence-trustworthiness question to the "
            "DERIVED_VERIFIER rule in onex_change_control, or to the OMN-14393 "
            "reproducibility direction."
        )
        if match is not None
        else ""
    )


@pytest.mark.unit
def test_forbidden_patterns_have_a_positive_control() -> None:
    """Every pattern must match text it is meant to catch.

    A ratchet whose regexes match nothing passes forever and proves nothing —
    the rule 16 failure mode. These are the shapes the retired comment used.
    """
    specimens = (
        "stays OFF pending OMN-14893 (sanctioned-automation identity wiring)",
        "signal (78.9% would-block) is already built and shadow-measured under",
        "the gate is already built and waiting",
        "blocked pending OMN-14893",
    )
    for pattern, _why in _FORBIDDEN_CLAIMS:
        assert any(
            re.search(pattern, specimen, re.IGNORECASE) for specimen in specimens
        ), f"pattern {pattern!r} matches none of its own specimens"


@pytest.mark.unit
def test_identity_gate_flag_is_never_live_code() -> None:
    """The flag may be discussed in a comment, never read as configuration.

    This is the half that would matter if someone re-implemented rather than
    re-described: a comment naming the flag is a record, a line of code reading
    it is the ruled-out gate coming back.
    """
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _GATE_FLAG not in line:
                continue
            if line.lstrip().startswith("#"):
                continue
            offenders.append(f"{path.relative_to(_SRC_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "the author-identity gate flag appears outside a comment (OMN-18778). "
        "That axis was ruled out on 2026-07-21 and must not be reintroduced "
        "here:\n" + "\n".join(offenders)
    )
