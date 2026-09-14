# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18332. The companion carries the author's own declaration, copied.

One test per acceptance-criterion falsifier on the ticket. The two that matter
most are the controls, and they are a matched pair:

* :class:`TestNegativeControlGamingSequence` replays the exact sequence the
  mechanism exists to refuse -- declare F1, watch F1 fail, edit the criterion to
  an already-passing F2, mint. A companion carrying an ACCEPTED binding there is
  the failure.
* :class:`TestPositiveControlUntouchedDeclaration` replays an untouched
  declaration. A HOLD there is equally the failure, because a mechanism that
  never releases anything delivers nothing.

The normalizer and per-criterion controls run over a committed snapshot of a
REAL ``documentContentHistory`` payload, not a hand-built document, because a
normalizer proven against a hand-built fixture only proves the fixture.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _draft_binding_labels,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_ac_bindings_block,
    render_accepted_ac_binding,
)
from omnimarket.occ_ac_transcription import (
    AUTOBINDER_IDENTITY,
    ModelTranscribedBinding,
    transcribe_ac_bindings,
)
from omnimarket.occ_creation_revision import (
    ModelCreationRevision,
    build_ticket_declaration,
    select_creation_revision,
)
from omnimarket.occ_criterion_normalizer import (
    markdown_comparison_text,
    rich_text_comparison_text,
)
from omnimarket.occ_criterion_units import (
    DEFAULT_CRITERION_POLICY,
    canonical_criterion_text,
    criterion_units,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ"
    / "omn_18332_real_document_content_history.json"
)

#: Three distinct instants, so no assertion below can pass by coincidence:
#: the ticket's creation, a later description edit, and a later mint. An
#: implementation that stamped the mint time, or the edit time, fails the
#: equality assertions rather than accidentally satisfying them.
CREATED_AT = datetime(2026, 9, 13, 18, 22, 13, 316000, tzinfo=UTC)
EDITED_AT = CREATED_AT + timedelta(hours=1)
MINTED_AT = CREATED_AT + timedelta(hours=2)

AUTHOR = "author-user-id"
EDITOR = "a-different-user-id"


def _doc(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "content": list(blocks)}


def _para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _heading(text: str, level: int = 2) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _bullets(*texts: str) -> dict[str, Any]:
    return {
        "type": "bullet_list",
        "content": [{"type": "list_item", "content": [_para(text)]} for text in texts],
    }


def _revision(
    doc: dict[str, Any], *, actor: str = AUTHOR, at: datetime = CREATED_AT
) -> ModelCreationRevision:
    return ModelCreationRevision(snapshot_at=at, content_data=doc, actor_id=actor)


_TWO_CRITERIA_MD = (
    "Some preamble.\n"
    "\n"
    "## Acceptance criteria\n"
    "\n"
    "* **AC1** the reader resolves the creation revision. "
    "— falsifier: run the reader against a fixture history\n"
    "* **AC2** an edited criterion is a draft. "
    "— falsifier: replay the edit sequence and assert no acceptor\n"
)

_TWO_CRITERIA_DOC = _doc(
    _para("Some preamble."),
    _heading("Acceptance criteria"),
    _bullets(
        "AC1 the reader resolves the creation revision. "
        "— falsifier: run the reader against a fixture history",
        "AC2 an edited criterion is a draft. "
        "— falsifier: replay the edit sequence and assert no acceptor",
    ),
)


def _transcribe(
    description: str,
    revision: ModelCreationRevision | None,
    *,
    creator_id: str | None = AUTHOR,
) -> tuple[ModelTranscribedBinding, ...]:
    return transcribe_ac_bindings(
        live_description=description,
        creation_revision=revision,
        created_at=CREATED_AT,
        creator_id=creator_id,
    )


def _by_label(
    records: tuple[ModelTranscribedBinding, ...],
) -> dict[str, ModelTranscribedBinding]:
    return {record.label: record for record in records}


# ---------------------------------------------------------------------------
# AC1 -- a real companion carries an accepted binding sourced from the ticket
# ---------------------------------------------------------------------------


class TestBindingsReachTheContract:
    """AC1. Both declared labels appear in the contract's binding records."""

    def test_both_declared_labels_appear_in_the_rendered_contract(self) -> None:
        records = _transcribe(_TWO_CRITERIA_MD, _revision(_TWO_CRITERIA_DOC))
        parsed = yaml.safe_load("  - id: item\n" + render_ac_bindings_block(records))
        item = parsed[0]

        assert item["binds_ac"] == ["AC1", "AC2"]
        assert [entry["label"] for entry in item["ac_bindings"]] == ["AC1", "AC2"]

    def test_an_empty_record_set_renders_a_contract_identical_to_todays(self) -> None:
        assert render_ac_bindings_block(()) == ""


# ---------------------------------------------------------------------------
# AC2 / AC2c -- acceptance names the creator and the creation time
# ---------------------------------------------------------------------------


class TestPositiveControlUntouchedDeclaration:
    """AC2 and AC2c. An untouched declaration is accepted and counts."""

    def test_acceptor_is_the_creator_and_the_time_is_the_creation_time(self) -> None:
        records = _transcribe(_TWO_CRITERIA_MD, _revision(_TWO_CRITERIA_DOC))

        assert len(records) == 2
        for record in records:
            assert record.is_accepted
            assert record.accepted_by == AUTHOR
            assert record.accepted_at == CREATED_AT
            # The two instants that must NOT appear anywhere.
            assert record.accepted_at != MINTED_AT
            assert record.accepted_at != EDITED_AT

    def test_the_closer_holds_nothing_on_an_untouched_declaration(self) -> None:
        records = _transcribe(_TWO_CRITERIA_MD, _revision(_TWO_CRITERIA_DOC))
        item = yaml.safe_load("  - id: item\n" + render_ac_bindings_block(records))[0]

        drafts = _draft_binding_labels(item, tuple(item["binds_ac"]))

        assert drafts == ()
        assert len(item["binds_ac"]) == 2


# ---------------------------------------------------------------------------
# AC2b / AC2d -- the exact gaming sequence, and why editor-attribution is not
# the fix
# ---------------------------------------------------------------------------


class TestNegativeControlGamingSequence:
    """AC2b. Declare F1, watch F1 fail, edit to a passing F2, mint after.

    The five steps are replayed literally. Step 2 -- running F1 and watching it
    fail -- is what makes this a gaming sequence rather than an ordinary edit,
    and it is represented by the fact that the mint happens AFTER the edit,
    which is the only part of it the transcriber can observe.
    """

    #: Step 1. The ticket is created declaring F1 against AC1, and AC2 alongside.
    CREATION_DOC = _doc(
        _heading("Acceptance criteria"),
        _bullets(
            "AC1 the resolver rejects a stale grant. "
            "— falsifier: uv run pytest tests/test_stale_grant.py",
            "AC2 the reader is fail-closed. "
            "— falsifier: uv run pytest tests/test_fail_closed.py",
        ),
    )

    #: Steps 3 and 4. AC1's falsifier is swapped for a check that already
    #: passes. AC2 is untouched, byte for byte.
    EDITED_MD = (
        "## Acceptance criteria\n"
        "\n"
        "* **AC1** the resolver rejects a stale grant. "
        "— falsifier: uv run pytest tests/test_imports.py\n"
        "* **AC2** the reader is fail-closed. "
        "— falsifier: uv run pytest tests/test_fail_closed.py\n"
    )

    def _records(self) -> dict[str, ModelTranscribedBinding]:
        # Step 5: the mint happens after the edit, and the edit is attributed to
        # a second actor at a later time.
        return _by_label(
            _transcribe(
                self.EDITED_MD,
                _revision(self.CREATION_DOC, actor=AUTHOR, at=CREATED_AT),
            )
        )

    def test_the_edited_criterion_is_a_draft_with_no_acceptance_at_all(self) -> None:
        record = self._records()["AC1"]

        assert not record.is_accepted
        assert record.accepted_by is None
        assert record.accepted_at is None
        assert record.proposed_by == AUTOBINDER_IDENTITY

    def test_ac2d_no_acceptance_record_names_the_editor(self) -> None:
        """AC2d. Recording the edit's actor is accurate AND still closes it.

        An implementation that attributed the acceptance to whoever made the
        edit would produce a factually correct record and let the sequence
        above close the ticket. This asserts the editor appears nowhere.
        """
        for record in self._records().values():
            assert record.accepted_by != EDITOR
            assert record.accepted_at != EDITED_AT

    def test_the_closer_holds_the_ticket_naming_that_criterion(self) -> None:
        records = tuple(self._records().values())
        item = yaml.safe_load("  - id: item\n" + render_ac_bindings_block(records))[0]

        drafts = _draft_binding_labels(item, tuple(item["binds_ac"]))

        assert drafts == ("AC1",)

    def test_a_transcriber_accepting_the_latest_revision_would_fail_both(self) -> None:
        """The same fixture against the WRONG rule, which must not pass.

        Feeding the edited document in as though it were the creation revision
        is precisely what a latest-revision transcriber does. It accepts the
        gamed criterion, which is why the two assertions above are the test.
        """
        edited_doc = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "AC1 the resolver rejects a stale grant. "
                "— falsifier: uv run pytest tests/test_imports.py",
                "AC2 the reader is fail-closed. "
                "— falsifier: uv run pytest tests/test_fail_closed.py",
            ),
        )
        wrong = _by_label(
            _transcribe(self.EDITED_MD, _revision(edited_doc, actor=EDITOR))
        )

        assert wrong["AC1"].is_accepted, (
            "the wrong rule must accept the gamed criterion; if it does not, "
            "this control is no longer discriminating between the two rules"
        )
        assert wrong["AC1"].accepted_by == EDITOR


# ---------------------------------------------------------------------------
# AC2e -- a neighbouring edit does not withdraw acceptance from its neighbours
# ---------------------------------------------------------------------------


class TestPerCriterionInvalidation:
    """AC2e. Editing criterion B leaves criterion A accepted."""

    def test_only_the_edited_criterion_is_demoted(self) -> None:
        creation = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "AC1 criterion A, untouched. — falsifier: check A",
                "AC2 criterion B, about to move. — falsifier: check B",
            ),
        )
        edited_md = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** criterion A, untouched. — falsifier: check A\n"
            "* **AC2** criterion B, reworded entirely. — falsifier: check B\n"
        )

        records = _by_label(_transcribe(edited_md, _revision(creation)))

        assert records["AC1"].is_accepted
        assert records["AC1"].accepted_by == AUTHOR
        assert not records["AC2"].is_accepted
        assert records["AC2"].accepted_by is None

    def test_the_closer_holds_on_b_alone(self) -> None:
        creation = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "AC1 criterion A, untouched. — falsifier: check A",
                "AC2 criterion B, about to move. — falsifier: check B",
            ),
        )
        edited_md = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** criterion A, untouched. — falsifier: check A\n"
            "* **AC2** criterion B, reworded entirely. — falsifier: check B\n"
        )
        records = _transcribe(edited_md, _revision(creation))
        item = yaml.safe_load("  - id: item\n" + render_ac_bindings_block(records))[0]

        assert _draft_binding_labels(item, tuple(item["binds_ac"])) == ("AC2",)

    def test_per_criterion_invalidation_on_the_real_captured_ticket(self) -> None:
        """The same property over a real edit that really happened.

        The captured ticket was created with five criteria and edited later to
        ADD a sixth. The five that existed at creation are byte-identical and
        must stay accepted; the added one has no creation text and must be a
        draft. An implementation that failed closed at DESCRIPTION granularity
        would emit six drafts and fail this.
        """
        payload = json.loads(_FIXTURE.read_text())
        issue = payload["issue"]
        created_at = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
        revision = select_creation_revision(payload["history"], created_at)
        assert revision is not None

        records = _by_label(
            transcribe_ac_bindings(
                live_description=issue["description"],
                creation_revision=revision,
                created_at=created_at,
                creator_id=issue["creator"]["id"],
            )
        )

        assert sorted(records) == ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"]
        for label in ("AC1", "AC2", "AC3", "AC4", "AC5"):
            assert records[label].is_accepted, f"{label} was never edited"
            assert records[label].accepted_at == created_at
        assert not records["AC6"].is_accepted, "AC6 was added after creation"


# ---------------------------------------------------------------------------
# AC2f -- the normalizer, proven on the real snapshot
# ---------------------------------------------------------------------------


class TestNormalizerControl:
    """AC2f. The hash unit is proven against a real revision, not asserted."""

    def test_the_real_markdown_and_the_real_revision_project_identically(self) -> None:
        """The measurement the plan asked for, on the production normalizer.

        The live markdown description and the LATEST rich-text revision are the
        same document in two formats, so their projections must be equal block
        for block. A straightforward block reconstruction managed five of
        thirteen on this payload; this asserts every block.
        """
        payload = json.loads(_FIXTURE.read_text())
        from_markdown = markdown_comparison_text(payload["issue"]["description"])
        from_rich_text = rich_text_comparison_text(payload["history"][0]["contentData"])

        assert from_markdown.splitlines() == from_rich_text.splitlines()

    def test_an_inline_markup_only_change_is_still_accepted(self) -> None:
        creation = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "AC1 the resolver reads scripts/validate_grant.py and refuses. "
                "— falsifier: run the validator"
            ),
        )
        # Emphasis added, a term wrapped in code formatting, a link introduced,
        # and the line reflowed. Same words.
        reformatted_md = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** the *resolver* reads `scripts/validate_grant.py`\n"
            "  and [refuses](https://example.invalid/spec).\n"
            "  — falsifier: run the validator\n"
        )

        records = _by_label(_transcribe(reformatted_md, _revision(creation)))

        assert records["AC1"].is_accepted

    def test_a_criterion_whose_wording_moved_is_not_accepted(self) -> None:
        creation = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "AC1 the resolver reads the grant and refuses. "
                "— falsifier: run the validator"
            ),
        )
        reworded_md = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** the resolver reads the grant and accepts. "
            "— falsifier: run the validator\n"
        )

        records = _by_label(_transcribe(reworded_md, _revision(creation)))

        assert not records["AC1"].is_accepted

    def test_a_swapped_falsifier_alone_is_not_accepted(self) -> None:
        """The falsifier is inside the hash, so swapping it is a rewrite.

        What the author accepted is the PAIR -- this criterion, settled by this
        check -- so a check changed under unchanged criterion prose must demote
        exactly as a reworded criterion does.
        """
        creation = _doc(
            _heading("Acceptance criteria"),
            _bullets("AC1 the resolver refuses. — falsifier: run the validator"),
        )
        swapped_md = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** the resolver refuses. — falsifier: run the import check\n"
        )

        records = _by_label(_transcribe(swapped_md, _revision(creation)))

        assert not records["AC1"].is_accepted


# ---------------------------------------------------------------------------
# AC2g -- an unresolvable creation revision auto-accepts nothing
# ---------------------------------------------------------------------------


class TestFailClosedEdge:
    """AC2g. Absence of the creation revision is never agreement."""

    def test_an_empty_history_emits_zero_acceptances_and_raises_nothing(self) -> None:
        assert select_creation_revision([], CREATED_AT) is None

        records = _transcribe(_TWO_CRITERIA_MD, None)

        assert len(records) == 2
        assert [record.is_accepted for record in records] == [False, False]
        assert all(record.accepted_by is None for record in records)

    def test_a_history_with_no_entry_at_the_creation_time_is_unresolved(self) -> None:
        history = [
            {
                "contentDataSnapshotAt": EDITED_AT.isoformat().replace("+00:00", "Z"),
                "actorIds": [EDITOR],
                "contentData": _TWO_CRITERIA_DOC,
            }
        ]

        assert select_creation_revision(history, CREATED_AT) is None

    def test_a_matching_entry_with_no_document_is_unresolved_not_empty(self) -> None:
        """A matched revision carrying no document must not read as "declared
        nothing" -- that would silently demote every criterion for a reason that
        is indistinguishable from a ticket with no criteria."""
        history = [
            {
                "contentDataSnapshotAt": CREATED_AT.isoformat().replace("+00:00", "Z"),
                "actorIds": [AUTHOR],
                "contentData": None,
            }
        ]

        assert select_creation_revision(history, CREATED_AT) is None

    def test_a_transport_that_returned_nothing_yields_no_declaration(self) -> None:
        """What the EFFECT hands over when every round trip failed."""
        assert build_ticket_declaration("OMN-1", None, None) is None

    def test_a_readable_issue_with_unreadable_history_still_returns_drafts(
        self,
    ) -> None:
        declaration = build_ticket_declaration(
            "OMN-1",
            {
                "id": "issue-1",
                "identifier": "OMN-1",
                "createdAt": CREATED_AT.isoformat().replace("+00:00", "Z"),
                "description": _TWO_CRITERIA_MD,
                "creator": {"id": AUTHOR},
                "documentContent": {"id": "doc-1"},
            },
            None,
        )

        assert declaration is not None
        assert declaration.creation_revision is None

        records = transcribe_ac_bindings(
            live_description=declaration.description,
            creation_revision=declaration.creation_revision,
            created_at=declaration.created_at,
            creator_id=declaration.creator_id,
        )

        assert records, "a readable description must still yield draft records"
        assert not any(record.is_accepted for record in records)

    def test_the_reader_resolves_the_creation_revision_end_to_end(self) -> None:
        """The positive control for the two transports above.

        Without it every fail-closed assertion here would also pass against a
        reader that always returns ``None``.
        """
        payload = json.loads(_FIXTURE.read_text())

        declaration = build_ticket_declaration(
            "OMN-FIXTURE", payload["issue"], payload["history"]
        )

        assert declaration is not None
        assert declaration.creation_revision is not None
        assert declaration.creation_revision.snapshot_at == declaration.created_at


# ---------------------------------------------------------------------------
# AC3 -- the transcriber is the proposer and never the acceptor
# ---------------------------------------------------------------------------


class TestProposerIsNeverTheAcceptor:
    """AC3. The two fields carry different things and cannot be conflated."""

    def test_every_record_names_the_autobinder_as_proposer_only(self) -> None:
        records = _transcribe(_TWO_CRITERIA_MD, _revision(_TWO_CRITERIA_DOC))

        for record in records:
            assert record.proposed_by == AUTOBINDER_IDENTITY
            assert record.accepted_by != AUTOBINDER_IDENTITY

    def test_the_renderer_refuses_an_acceptor_equal_to_the_proposer(self) -> None:
        with pytest.raises(ValueError, match="never be recorded as the acceptor"):
            render_accepted_ac_binding(
                label="AC1",
                criterion_hash=hashlib.sha256(b"x").hexdigest(),
                proposed_by=AUTOBINDER_IDENTITY,
                accepted_by=AUTOBINDER_IDENTITY,
                accepted_at=CREATED_AT,
            )

    def test_the_model_refuses_a_half_populated_acceptance(self) -> None:
        with pytest.raises(ValueError, match="an actor AND a time"):
            ModelTranscribedBinding(
                label="AC1",
                criterion_hash=hashlib.sha256(b"x").hexdigest(),
                falsifier="run it",
                proposed_by=AUTOBINDER_IDENTITY,
                accepted_by=AUTHOR,
            )

    def test_a_revision_whose_only_actor_is_the_transcriber_is_not_accepted(
        self,
    ) -> None:
        records = _by_label(
            _transcribe(
                _TWO_CRITERIA_MD,
                _revision(_TWO_CRITERIA_DOC, actor=AUTOBINDER_IDENTITY),
                creator_id=None,
            )
        )

        assert not records["AC1"].is_accepted


# ---------------------------------------------------------------------------
# AC4 -- the hash pins the text the acceptance names
# ---------------------------------------------------------------------------


class TestCriterionHashPin:
    """AC4. A criterion rewritten after acceptance breaks its own pin."""

    CREATION = _doc(
        _heading("Acceptance criteria"),
        _bullets(
            "AC1 first criterion. — falsifier: check one",
            "AC2 second criterion. — falsifier: check two",
        ),
    )
    FIRST_MD = (
        "## Acceptance criteria\n"
        "\n"
        "* **AC1** first criterion. — falsifier: check one\n"
        "* **AC2** second criterion. — falsifier: check two\n"
    )
    MUTATED_MD = (
        "## Acceptance criteria\n"
        "\n"
        "* **AC1** first criterion, now saying something else. "
        "— falsifier: check one\n"
        "* **AC2** second criterion. — falsifier: check two\n"
    )

    def test_mutating_a_criterion_between_two_mints_changes_its_hash(self) -> None:
        first = _by_label(_transcribe(self.FIRST_MD, _revision(self.CREATION)))
        second = _by_label(_transcribe(self.MUTATED_MD, _revision(self.CREATION)))

        assert first["AC1"].criterion_hash != second["AC1"].criterion_hash

    def test_mutating_a_different_criterion_leaves_this_hash_unchanged(self) -> None:
        first = _by_label(_transcribe(self.FIRST_MD, _revision(self.CREATION)))
        second = _by_label(_transcribe(self.MUTATED_MD, _revision(self.CREATION)))

        assert first["AC2"].criterion_hash == second["AC2"].criterion_hash

    def test_the_accepted_hash_is_the_creation_revision_hash(self) -> None:
        records = _by_label(_transcribe(self.FIRST_MD, _revision(self.CREATION)))
        creation_units = {
            unit.label: unit.criterion_hash
            for unit in criterion_units(
                rich_text_comparison_text(self.CREATION), DEFAULT_CRITERION_POLICY
            )
        }

        assert records["AC1"].criterion_hash == creation_units["AC1"]

    def test_the_hash_is_the_admission_guards_hash_over_identical_text(self) -> None:
        """The pin is worthless if two parsers disagree about the text.

        This recomputes the digest the way the vendored guard code does and
        asserts the transcriber emitted exactly that, so the transcriber cannot
        quietly acquire a hashing rule of its own.
        """
        records = _by_label(_transcribe(self.FIRST_MD, _revision(self.CREATION)))
        unit = next(
            unit
            for unit in criterion_units(
                markdown_comparison_text(self.FIRST_MD), DEFAULT_CRITERION_POLICY
            )
            if unit.label == "AC1"
        )
        expected = hashlib.sha256(
            canonical_criterion_text(unit.text).encode("utf-8")
        ).hexdigest()

        assert records["AC1"].criterion_hash == expected


# ---------------------------------------------------------------------------
# AC5 -- an undeclared ticket produces no binding at all
# ---------------------------------------------------------------------------


class TestUndeclaredTicketProducesNothing:
    """AC5. No declaration, no binding, no invention."""

    BARE_MD = (
        "## Acceptance criteria\n"
        "\n"
        "* **AC1** the thing works.\n"
        "* **AC2** the other thing works.\n"
    )
    BARE_DOC = _doc(
        _heading("Acceptance criteria"),
        _bullets("AC1 the thing works.", "AC2 the other thing works."),
    )

    def test_bare_criteria_yield_no_records_at_all(self) -> None:
        assert _transcribe(self.BARE_MD, _revision(self.BARE_DOC)) == ()

    def test_a_description_with_no_criteria_section_yields_nothing(self) -> None:
        assert _transcribe("Just prose, no section.", _revision(_doc(_para("x")))) == ()

    def test_an_unlabelled_criterion_is_skipped_rather_than_bound(self) -> None:
        """A binding needs something stable to point at.

        A position-derived ordinal renumbers every binding below it the moment
        a bullet is inserted, so an unlabelled criterion is left for the closer
        to report rather than bound to a number.
        """
        md = (
            "## Acceptance criteria\n"
            "\n"
            "* the thing works. — falsifier: run it\n"
            "* **AC2** the other thing. — falsifier: run other\n"
        )
        doc = _doc(
            _heading("Acceptance criteria"),
            _bullets(
                "the thing works. — falsifier: run it",
                "AC2 the other thing. — falsifier: run other",
            ),
        )

        records = _transcribe(md, _revision(doc))

        assert [record.label for record in records] == ["AC2"]


# ---------------------------------------------------------------------------
# AC1 end to end -- the bindings reach the rendered COMPANION CONTRACT
# ---------------------------------------------------------------------------


class TestRenderedCompanionContract:
    """AC1. The contract a mint would commit carries the bindings."""

    def _records(self) -> tuple[ModelTranscribedBinding, ...]:
        return _transcribe(_TWO_CRITERIA_MD, _revision(_TWO_CRITERIA_DOC))

    def test_the_fresh_path_contract_declares_both_bindings(self) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
            render_compute_companion_contract,
        )

        text = render_compute_companion_contract(
            ticket_id="OMN-18332",
            repo="OmniNode-ai/omnimarket",
            pr_number=1234,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-1234",
            ac_bindings=self._records(),
        )
        item = yaml.safe_load(text)["dod_evidence"][0]

        assert item["binds_ac"] == ["AC1", "AC2"]
        accepted = {entry["label"]: entry for entry in item["ac_bindings"]}
        assert accepted["AC1"]["accepted_by"] == AUTHOR
        assert accepted["AC1"]["proposed_by"] == AUTOBINDER_IDENTITY
        assert accepted["AC1"]["accepted_at"].startswith("2026-09-13T18:22:13")

    def test_no_bindings_renders_the_contract_this_producer_renders_today(
        self,
    ) -> None:
        """The regression control on every pre-cutover ticket.

        A ticket that declared no falsifiers must produce bytes identical to
        today's, not a contract with an empty key in it.
        """
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
            render_compute_companion_contract,
        )

        kwargs: dict[str, Any] = {
            "ticket_id": "OMN-18332",
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 1234,
            "evidence_id": "dod-OmniNode-ai-omnimarket-pr-1234",
        }

        assert render_compute_companion_contract(
            **kwargs, ac_bindings=()
        ) == render_compute_companion_contract(**kwargs)

    def test_the_self_bind_suffix_property_survives_the_new_key(self) -> None:
        """The merged path subtracts base from full to isolate the self-bind item.

        That subtraction is only valid while both renders are byte-identical
        apart from that entry. The binding block is emitted on both, so this
        asserts the suffix is still exactly one dod_evidence item.
        """
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
            render_compute_companion_contract,
        )

        common: dict[str, Any] = {
            "ticket_id": "OMN-18332",
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 1234,
            "evidence_id": "dod-OmniNode-ai-omnimarket-pr-1234",
            "ac_bindings": self._records(),
        }
        base = render_compute_companion_contract(**common)
        full = render_compute_companion_contract(
            **common,
            self_bind_evidence_id="occ-self-bind-9999",
            occ_pr_number=9999,
            occ_repo="OmniNode-ai/onex_change_control",
        )

        assert full.startswith(base)
        suffix = yaml.safe_load("dod_evidence:\n" + full[len(base) :])
        assert len(suffix["dod_evidence"]) == 1
