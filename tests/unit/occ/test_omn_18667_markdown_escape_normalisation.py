# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18667. A markdown escape is not an edit, and must not read as one.

The comparison projection answers one question: has this criterion's text MOVED
since the ticket was created? The two sides arrive in different formats, and the
markdown side is produced by Linear's own serializer, which backslash-escapes a
character that would otherwise be read as block structure at the start of a
line. The rich-text side is a ProseMirror document whose text nodes hold the
literal characters and carry no escaping at all.

So a criterion that WRAPS onto a line beginning with an escapable token gets a
backslash on one side of the comparison and not the other, and an UNEDITED
criterion reads as moved. Its acceptance is then silently withheld, and the
ticket holds on ``gap_ac_unbound`` forever, because no human edit can clear it:
the human never made one.

Measured on OMN-18620, whose ``documentContentHistory`` has exactly ONE entry,
at a ``contentDataSnapshotAt`` equal to its ``createdAt`` -- so nobody ever
edited that description. Five of its six criteria hashed EQUAL across the two
renderings and AC5 alone differed, by exactly one character. That ticket's real
payload is the fixture below, because a normalizer proven against a hand-built
fixture only proves the fixture.

The two controls are a matched pair and neither alone is the property:

* :class:`TestUneditedCriterionIsNotMoved` -- the live fixture's AC5 compares
  EQUAL and transcribes as ACCEPTED. A hold there is the failure this ticket
  exists to remove.
* :class:`TestRealEditIsStillMoved` -- a criterion whose WORDING was actually
  changed still compares as moved and still transcribes as a DRAFT. A pass
  there would mean the fix bought acceptance by widening the comparison, which
  would be strictly worse than the bug: it would accept the gaming sequence the
  whole mechanism exists to refuse.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.occ_ac_transcription import (
    AUTOBINDER_IDENTITY,
    transcribe_ac_bindings,
)
from omnimarket.occ_creation_revision import (
    ModelCreationRevision,
    select_creation_revision,
)
from omnimarket.occ_criterion_normalizer import (
    markdown_comparison_text,
    rich_text_comparison_text,
)
from omnimarket.occ_criterion_units import (
    DEFAULT_CRITERION_POLICY,
    criterion_units,
)

pytestmark = pytest.mark.unit

#: The real OMN-18620 payload: the live ``description`` markdown and the single
#: ``documentContentHistory`` entry, fetched through the same two GraphQL
#: queries the read-EFFECT issues (``ISSUE_QUERY`` / ``HISTORY_QUERY`` in
#: :mod:`omnimarket.occ_creation_revision`).
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ"
    / "omn_18667_omn_18620_document_content_history.json"
)

#: The criterion that the escape round trip held. Named rather than discovered
#: so a fixture whose shape changed fails loudly instead of testing nothing.
HELD_LABEL = "AC5"


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


def _live_description() -> str:
    description = _fixture()["issue"]["description"]
    assert isinstance(description, str)
    assert description
    return description


def _creation_revision() -> ModelCreationRevision:
    payload = _fixture()
    created_at = datetime.fromisoformat(
        str(payload["issue"]["createdAt"]).replace("Z", "+00:00")
    )
    revision = select_creation_revision(payload["history"], created_at)
    assert revision is not None, "the fixture's creation revision must resolve"
    return revision


def _created_at() -> datetime:
    return datetime.fromisoformat(
        str(_fixture()["issue"]["createdAt"]).replace("Z", "+00:00")
    )


def _hashes(comparison_text: str) -> dict[str, str]:
    return {
        unit.label: unit.criterion_hash
        for unit in criterion_units(comparison_text, DEFAULT_CRITERION_POLICY)
        if unit.label is not None
    }


def _live_hashes(description: str) -> dict[str, str]:
    return _hashes(markdown_comparison_text(description))


def _creation_hashes(content_data: dict[str, Any]) -> dict[str, str]:
    return _hashes(rich_text_comparison_text(content_data))


class TestFixtureIsTheMeasuredCase:
    """The fixture is the ticket the defect was measured on, unedited."""

    def test_history_has_exactly_one_entry_at_creation(self) -> None:
        payload = _fixture()
        history = payload["history"]
        assert len(history) == 1, (
            "the whole point of this fixture is a description NOBODY edited; a "
            "second revision would make the comparison legitimately unequal"
        )
        assert history[0]["contentDataSnapshotAt"] == payload["issue"]["createdAt"]

    def test_the_held_criterion_wraps_onto_an_escaped_token(self) -> None:
        """The live markdown really does carry the serializer's escape."""
        lines = _live_description().splitlines()
        escaped = [line for line in lines if line.lstrip().startswith("\\--")]
        assert escaped, (
            "the fixture must still contain the wrapped line whose leading "
            "double hyphen Linear escaped; without it this file tests nothing"
        )


class TestUneditedCriterionIsNotMoved:
    """RED before the fix: AC5 alone differed, by exactly one character."""

    def test_every_criterion_compares_equal(self) -> None:
        live = _live_hashes(_live_description())
        creation = _creation_hashes(_creation_revision().content_data)
        assert set(live) == set(creation)
        moved = sorted(label for label in live if live[label] != creation[label])
        assert moved == [], (
            "no criterion of this description was ever edited, so none may "
            f"read as moved; these did: {moved}"
        )

    def test_the_held_criterion_transcribes_as_accepted(self) -> None:
        """The end-to-end property: AC5 stops being an OMN-18238 draft."""
        records = transcribe_ac_bindings(
            live_description=_live_description(),
            creation_revision=_creation_revision(),
            created_at=_created_at(),
            creator_id=_fixture()["issue"]["creator"]["id"],
        )
        by_label = {record.label: record for record in records}
        assert HELD_LABEL in by_label, (
            f"{HELD_LABEL} declares a falsifier and must yield a record"
        )
        held = by_label[HELD_LABEL]
        assert held.is_accepted, (
            f"{HELD_LABEL} was never edited; withholding its acceptance holds "
            "OMN-18620 on gap_ac_unbound with no human act available to clear it"
        )
        assert held.accepted_by == _fixture()["issue"]["creator"]["id"]
        assert held.accepted_by != AUTOBINDER_IDENTITY
        assert held.accepted_at == _created_at()

    def test_no_criterion_is_left_a_draft(self) -> None:
        records = transcribe_ac_bindings(
            live_description=_live_description(),
            creation_revision=_creation_revision(),
            created_at=_created_at(),
            creator_id=_fixture()["issue"]["creator"]["id"],
        )
        assert records, "the fixture declares criteria with falsifiers"
        drafts = sorted(r.label for r in records if not r.is_accepted)
        assert drafts == [], f"unedited criteria left as drafts: {drafts}"


class TestRealEditIsStillMoved:
    """The negative control. Acceptance is never bought by widening."""

    def test_a_reworded_criterion_still_reads_as_moved(self) -> None:
        edited = _live_description().replace(
            "the rotation guard's own test suite run green with both forms",
            "the rotation guard's own test suite run green with EITHER form",
        )
        assert edited != _live_description(), "the edit must actually apply"
        live = _live_hashes(edited)
        creation = _creation_hashes(_creation_revision().content_data)
        assert live[HELD_LABEL] != creation[HELD_LABEL], (
            "a criterion whose WORDING moved must still read as moved -- that "
            "is the property the acceptance mechanism rests on"
        )

    def test_a_reworded_criterion_still_transcribes_as_a_draft(self) -> None:
        edited = _live_description().replace(
            "both existing resolvers accept the timestamp form",
            "both existing resolvers accept the epoch form",
        )
        records = transcribe_ac_bindings(
            live_description=edited,
            creation_revision=_creation_revision(),
            created_at=_created_at(),
            creator_id=_fixture()["issue"]["creator"]["id"],
        )
        by_label = {record.label: record for record in records}
        assert not by_label[HELD_LABEL].is_accepted, (
            "the gaming sequence OMN-18238 exists to refuse -- edit the "
            "criterion, then mint -- must still yield a DRAFT"
        )

    def test_an_unrelated_criterion_edit_does_not_move_its_neighbours(self) -> None:
        edited = _live_description().replace(
            "both existing resolvers accept the timestamp form",
            "both existing resolvers accept the epoch form",
        )
        live = _live_hashes(edited)
        creation = _creation_hashes(_creation_revision().content_data)
        moved = sorted(label for label in live if live[label] != creation[label])
        assert moved == [HELD_LABEL], (
            "acceptance is settled per criterion, not per description; an edit "
            f"to {HELD_LABEL} must leave every other criterion accepted"
        )


class TestEscapeNormalisationIsTheSerializersInverse:
    """Unit coverage of the escapes Linear's serializer actually writes."""

    @pytest.mark.parametrize(
        ("escaped", "literal"),
        [
            ("\\-- falsifier: the check runs", "-- falsifier: the check runs"),
            ("\\- a dashed continuation", "- a dashed continuation"),
            ("\\* a starred continuation", "* a starred continuation"),
            ("\\+ a plussed continuation", "+ a plussed continuation"),
            ("\\# not a heading, just a hash", "# not a heading, just a hash"),
            ("\\_underscored\\_ continuation", "_underscored_ continuation"),
            ("1\\. not an ordinal", "1. not an ordinal"),
            ("\\: a colon continuation", ": a colon continuation"),
            ("a \\[bracketed\\] token", "a [bracketed] token"),
            ("a \\`backticked\\` token", "a `backticked` token"),
            ("a \\~tilde\\~ token", "a ~tilde~ token"),
        ],
    )
    def test_escaped_markdown_matches_the_literal_rich_text(
        self, escaped: str, literal: str
    ) -> None:
        """A paragraph the serializer escaped projects onto the literal text."""
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": literal}],
                }
            ],
        }
        assert markdown_comparison_text(escaped) == rich_text_comparison_text(rich)

    def test_an_escaped_backslash_survives_as_one_backslash(self) -> None:
        """``\\\\`` is a literal backslash on both sides, not a dropped one.

        This is why the unescape belongs on the MARKDOWN side alone. Applying
        the same pass to the rich-text side would turn an author's literal
        ``\\-`` into ``-`` and reintroduce the very asymmetry it was added to
        remove -- in the opposite direction.
        """
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "a literal \\- backslash"}],
                }
            ],
        }
        assert markdown_comparison_text(
            "a literal \\\\\\- backslash"
        ) == rich_text_comparison_text(rich)

    def test_a_code_span_is_literal_and_is_not_unescaped(self) -> None:
        """Backslash escapes do not apply inside a code span, per CommonMark.

        A criterion citing a regex is the realistic case, and unescaping inside
        the span would drop a character the rich-text side keeps.
        """
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "matches "},
                        {
                            "type": "text",
                            "text": "\\d+\\.",
                            "marks": [{"type": "code"}],
                        },
                        {"type": "text", "text": " exactly"},
                    ],
                }
            ],
        }
        assert markdown_comparison_text(
            "matches `\\d+\\.` exactly"
        ) == rich_text_comparison_text(rich)


class TestStructureIsNotLaundered:
    """An escaped marker is text; it must not become block structure."""

    def test_an_escaped_bullet_is_not_read_as_a_list_item(self) -> None:
        """``\\- item`` is a paragraph, and projects as the rich text does."""
        paragraph: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "- item"}],
                }
            ],
        }
        assert markdown_comparison_text("\\- item") == rich_text_comparison_text(
            paragraph
        )

    def test_a_real_bullet_still_projects_as_a_list_item(self) -> None:
        """And the two are still told apart, which is the whole point."""
        assert markdown_comparison_text("- item") != markdown_comparison_text(
            "\\- item"
        )
        assert markdown_comparison_text("- item") == "* item"

    def test_an_escaped_hash_projects_as_the_rich_text_paragraph_does(self) -> None:
        """An escaped hash is not read as a heading, and now matches its peer.

        It does however project onto the same string a REAL heading of that
        level projects onto, because the projection keeps the ``#`` run
        verbatim. That collision is pre-existing and, crucially, SYMMETRIC: on
        the rich-text side a paragraph whose literal text is ``### X`` and a
        level-3 heading reading ``X`` already projected identically, before
        this change and after it. A collision present on both sides of the
        comparison cannot make a criterion read as moved, which is the only
        question this projection answers.

        What DID change is the half that was asymmetric. Against ``origin/dev``
        the escaped-hash markdown projected to ``\\### X`` while the rich-text
        paragraph projected to ``### X``, so the two disagreed -- the same
        defect as the wrapped double hyphen, on a different token.
        """
        paragraph: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "### Acceptance criteria"}],
                }
            ],
        }
        heading: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "Acceptance criteria"}],
                }
            ],
        }
        assert markdown_comparison_text(
            "\\### Acceptance criteria"
        ) == rich_text_comparison_text(paragraph)
        # The pre-existing symmetric collision, pinned so that a later change
        # which breaks the symmetry fails here rather than silently holding a
        # ticket.
        assert rich_text_comparison_text(paragraph) == rich_text_comparison_text(
            heading
        )

    def test_an_escaped_asterisk_is_not_stripped_as_emphasis(self) -> None:
        """The escaped character must survive the inline-markup strippers.

        Unescaping before they run would hand them a bare ``*`` to delete;
        unescaping after would leave the orphaned backslash behind. Either way
        the character the author typed goes missing on one side only.
        """
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "a * b * c"}],
                }
            ],
        }
        assert markdown_comparison_text("a \\* b \\* c") == rich_text_comparison_text(
            rich
        )

    def test_emphasis_is_still_stripped_when_it_is_not_escaped(self) -> None:
        """The projection has not stopped discarding real inline formatting."""
        assert markdown_comparison_text("a **bold** word") == "a bold word"


#: The SECOND defect, found while this ticket's own AC1 stayed unbound after
#: the escape fix landed, and pinned against this ticket's own real payload.
_OWN_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ"
    / "omn_18667_own_ticket_document_content_history.json"
)


class TestCodeSpanContentIsLiteralOnBothSides:
    """A second, distinct asymmetry in the same function.

    ``_CODE_SPAN`` unwraps a span to its content, and ``_EMPHASIS`` then runs
    over the WHOLE line -- so an asterisk that was INSIDE backticks is deleted
    once the backticks protecting it are gone. The module's own comment says
    the ordering exists to prevent exactly that; unwrapping first defeats it.
    The rich-text side drops the ``code`` mark and keeps the text verbatim, so
    the two sides disagree and an unedited criterion reads as moved.

    No backslash is required to trigger it. It is not the escape defect, it was
    not fixed by the escape fix, and it held THIS ticket's own AC1.
    """

    def test_a_plain_code_span_keeps_its_asterisk(self) -> None:
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "the "},
                        {"type": "text", "text": "a*b", "marks": [{"type": "code"}]},
                        {"type": "text", "text": " token"},
                    ],
                }
            ],
        }
        assert markdown_comparison_text("the `a*b` token") == rich_text_comparison_text(
            rich
        )

    def test_a_code_span_keeps_bracket_and_paren_runs(self) -> None:
        """``_LINK`` would otherwise eat a link-shaped token inside a span."""
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "[a](b)",
                            "marks": [{"type": "code"}],
                        },
                    ],
                }
            ],
        }
        assert markdown_comparison_text("`[a](b)`") == rich_text_comparison_text(rich)

    def test_this_tickets_own_ac1_compares_equal(self) -> None:
        """The live case: ``at least `\\-`, `\\*`, `\\_`, `\\#` at line start``.

        Before the code-span fix the markdown side projected that as
        ``\\-, \\, \\, \\#`` -- the ``*`` and the ``_`` deleted by ``_EMPHASIS``
        after their backticks were unwrapped -- while the rich-text side kept
        all four. AC1 read as moved and was refused, on a ticket nobody had
        edited.
        """
        payload = json.loads(_OWN_FIXTURE.read_text())
        assert len(payload["history"]) == 1
        created_at = datetime.fromisoformat(
            str(payload["issue"]["createdAt"]).replace("Z", "+00:00")
        )
        revision = select_creation_revision(payload["history"], created_at)
        assert revision is not None
        live = _live_hashes(payload["issue"]["description"])
        creation = _creation_hashes(revision.content_data)
        moved = sorted(label for label in live if live[label] != creation[label])
        assert moved == [], f"unedited criteria reading as moved: {moved}"

    def test_this_tickets_own_criteria_all_transcribe_as_accepted(self) -> None:
        payload = json.loads(_OWN_FIXTURE.read_text())
        created_at = datetime.fromisoformat(
            str(payload["issue"]["createdAt"]).replace("Z", "+00:00")
        )
        records = transcribe_ac_bindings(
            live_description=payload["issue"]["description"],
            creation_revision=select_creation_revision(payload["history"], created_at),
            created_at=created_at,
            creator_id=payload["issue"]["creator"]["id"],
        )
        assert records
        drafts = sorted(r.label for r in records if not r.is_accepted)
        assert drafts == [], f"unedited criteria left as drafts: {drafts}"


class TestUnescapingIsOtherwiseInert:
    """A body with no escapes projects byte-for-byte as it did before."""

    def test_the_fixture_body_without_escapes_is_unchanged(self) -> None:
        stripped = _live_description().replace("\\--", "--")
        assert "\\" not in stripped
        # The escape-free form and the escaped form must agree -- that IS the
        # fix -- and the escape-free form is also what the pre-fix projection
        # produced for it, so nothing else in the body moved.
        assert markdown_comparison_text(stripped) == markdown_comparison_text(
            _live_description()
        )

    def test_a_backslash_before_a_non_punctuation_char_is_kept(self) -> None:
        """Only ASCII punctuation is escapable; ``\\d`` is a literal backslash."""
        rich: dict[str, Any] = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "a \\d token"}],
                }
            ],
        }
        assert markdown_comparison_text("a \\d token") == rich_text_comparison_text(
            rich
        )
