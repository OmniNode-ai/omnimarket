# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19038. The criterion TEXT survives the mint, not just its digest.

The transcriber reads the ticket body, splits it into criterion units, and
hashes each one. Then it throws the words away: ``ModelTranscribedBinding``
carried ``label``, ``criterion_hash`` and ``falsifier``, and ``unit.text`` --
the only copy of the criterion's actual sentence anywhere in the pipeline --
reached no field of the rendered contract.

Measured on ``origin/dev`` 2026-09-21 at ``605ebfd729``: 9,300 contracts under
``onex_change_control/contracts/``, **370** carrying a ``binds_ac`` entry, and
exactly **one** carrying a ``requirements`` key. So 370 live bindings name
criteria whose text exists nowhere in the contract corpus, and the OMN-18270
serializer -- which renders ``requirements[].acceptance[]`` into the ticket
body and treats the contract as the model -- has almost no input.

This module pins the fix at the producer. The load-bearing property is not
"a ``requirements`` key is present"; a renderer that emitted an empty one, or
one built from a different filter than the bindings, would satisfy that and
would be worse than nothing, because the serializer refuses a contract binding
a label its model does not enumerate (``ac_section_unenumerated_binding``) and
a model that disagrees with its bindings turns that refusal into a permanent
one. The property is:

    ``binds_ac`` and ``requirements[].acceptance[]`` are two renderings of ONE
    filtered unit list, so the declared set and the bound set are equal BY
    CONSTRUCTION and cannot drift.

``TestDeclaredSetEqualsBoundSet`` asserts that as an identity over the parsed
contract rather than as two presence checks, because two presence checks pass
on any overlap at all.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_companion_contract,
    render_compute_companion_contract,
    render_requirements_block,
)
from omnimarket.occ_ac_transcription import (
    AUTOBINDER_IDENTITY,
    ModelTranscribedBinding,
    transcribe_ac_bindings,
)
from omnimarket.occ_criterion_units import DEFAULT_CRITERION_POLICY, criterion_units

pytestmark = pytest.mark.unit


_CREATED_AT = datetime(2026, 9, 13, 21, 57, 10, tzinfo=UTC)

#: A body in the shape the corpus actually carries: a heading, checkbox items,
#: a label, a falsifier per criterion. AC3 deliberately declares NO falsifier,
#: which is the population the transcriber refuses to bind.
BODY = """## Acceptance criteria

- [ ] AC1: the gap lookup is index-backed rather than a sequential scan -- falsifier: an integration test EXPLAINs the query and fails if the plan contains Seq Scan
- [ ] AC2: the writer materialises rows again on the dev plane -- falsifier: max(window_start) advances past the frozen timestamp, read twice ten minutes apart
- [ ] AC3: somebody should look at the dashboard at some point
"""


def _statement_of(label: str) -> str:
    """The canonical criterion text the transcriber hashes, read independently."""
    for unit in criterion_units(BODY, DEFAULT_CRITERION_POLICY):
        if unit.label == label:
            return unit.text
    msg = f"fixture does not carry {label}"
    raise AssertionError(msg)


def _record(label: str, statement: str) -> ModelTranscribedBinding:
    return ModelTranscribedBinding(
        label=label,
        criterion_hash=hashlib.sha256(statement.encode("utf-8")).hexdigest(),
        statement=statement,
        falsifier=f"pytest -k {label}",
        proposed_by=AUTOBINDER_IDENTITY,
    )


def _contract_with(*records: ModelTranscribedBinding) -> str:
    return render_companion_contract(
        ticket_id="OMN-19038",
        repo="OmniNode-ai/omnimarket",
        pr_number=2754,
        evidence_id="dod-OmniNode-ai-omnimarket-pr-2754",
        ac_bindings=records,
    )


def _parsed(rendered: str) -> dict[str, object]:
    loaded = yaml.safe_load(rendered)
    assert isinstance(loaded, dict), "a contract must parse as a mapping"
    return loaded


def _acceptance_ids(contract: dict[str, object]) -> list[str]:
    requirements = contract.get("requirements")
    assert isinstance(requirements, list), (
        f"contract carries no requirements list; got {type(requirements)!r}"
    )
    ids: list[str] = []
    for requirement in requirements:
        assert isinstance(requirement, dict)
        acceptance = requirement.get("acceptance")
        assert isinstance(acceptance, list)
        for criterion in acceptance:
            assert isinstance(criterion, dict)
            ids.append(str(criterion["id"]))
    return ids


def _bound_labels(contract: dict[str, object]) -> list[str]:
    evidence = contract.get("dod_evidence")
    assert isinstance(evidence, list)
    labels: list[str] = []
    for item in evidence:
        assert isinstance(item, dict)
        for entry in item.get("binds_ac") or []:
            labels.append(str(entry))
    return labels


class TestTheContractCarriesTheCriterionText:
    """The headline: the words reach the contract, not only the digest."""

    def test_rendered_contract_enumerates_the_bound_criteria(self) -> None:
        contract = _parsed(
            _contract_with(
                _record("AC1", _statement_of("AC1")),
                _record("AC2", _statement_of("AC2")),
            )
        )
        assert _acceptance_ids(contract) == ["AC1", "AC2"]

    def test_the_statement_is_the_text_the_transcriber_hashed(self) -> None:
        """Not a paraphrase, not a truncation, not the falsifier alone.

        Asserted against ``criterion_units`` read independently from the same
        body, so a renderer that wrote the label, the falsifier or an empty
        string into ``statement`` fails here rather than passing a presence
        check.
        """
        contract = _parsed(_contract_with(_record("AC1", _statement_of("AC1"))))
        requirements = contract["requirements"]
        assert isinstance(requirements, list)
        first = requirements[0]
        assert isinstance(first, dict)
        acceptance = first["acceptance"]
        assert isinstance(acceptance, list)
        criterion = acceptance[0]
        assert isinstance(criterion, dict)
        assert criterion["statement"] == _statement_of("AC1")
        assert "Seq Scan" in str(criterion["statement"])

    def test_the_requirement_itself_carries_a_non_empty_statement(self) -> None:
        """``ModelRequirement.statement`` is required and must not be blank.

        A requirement rendered with an empty statement parses, reaches the
        serializer, and fails validation in ``omnibase_core`` far from here.
        """
        contract = _parsed(_contract_with(_record("AC1", _statement_of("AC1"))))
        requirements = contract["requirements"]
        assert isinstance(requirements, list)
        first = requirements[0]
        assert isinstance(first, dict)
        assert str(first["id"]).strip()
        assert str(first["statement"]).strip()


class TestDeclaredSetEqualsBoundSet:
    """The identity, asserted as an identity. Two presence checks are not this."""

    def test_every_bound_label_is_enumerated_and_nothing_else_is(self) -> None:
        contract = _parsed(
            _contract_with(
                _record("AC1", _statement_of("AC1")),
                _record("AC2", _statement_of("AC2")),
            )
        )
        assert set(_acceptance_ids(contract)) == set(_bound_labels(contract))

    def test_a_criterion_with_no_falsifier_is_in_neither(self) -> None:
        """AC3 declares no falsifier, so the transcriber mints no binding for it.

        It must therefore be absent from the model too. A producer that
        enumerated every criterion in the body while binding only the
        falsifiable ones would declare a criterion nothing claims, which the
        OMN-18333 rule ``ac_binding_criterion_unbound`` refuses.
        """
        records = transcribe_ac_bindings(
            live_description=BODY,
            creation_revision=None,
            created_at=_CREATED_AT,
        )
        assert [record.label for record in records] == ["AC1", "AC2"]
        contract = _parsed(_contract_with(*records))
        assert "AC3" not in _acceptance_ids(contract)
        assert "AC3" not in _bound_labels(contract)
        assert set(_acceptance_ids(contract)) == set(_bound_labels(contract))

    def test_a_label_the_consumers_reader_cannot_resolve_is_in_neither(self) -> None:
        """A zero-padded ordinal is the measured case, and it must drop from both.

        ``criterion_units`` reads ``AC01`` verbatim while the consumer's reader
        canonicalises it to ``AC1``, so the pin lookup misses and the
        transcriber mints nothing. The model must agree: enumerating a
        criterion under a label the gate cannot find is an
        ``ac_binding_unknown_criterion`` refusal waiting to happen.
        """
        padded = BODY.replace("AC1:", "AC01:")
        records = transcribe_ac_bindings(
            live_description=padded,
            creation_revision=None,
            created_at=_CREATED_AT,
        )
        labels = [record.label for record in records]
        assert "AC01" not in labels
        contract = _parsed(_contract_with(*records))
        assert "AC01" not in _acceptance_ids(contract)
        assert set(_acceptance_ids(contract)) == set(_bound_labels(contract))


class TestTheTranscriberCarriesTheStatement:
    """The text must survive the transcriber, which is where it was dropped."""

    def test_transcribed_records_carry_the_canonical_criterion_text(self) -> None:
        records = transcribe_ac_bindings(
            live_description=BODY,
            creation_revision=None,
            created_at=_CREATED_AT,
        )
        assert [record.statement for record in records] == [
            _statement_of("AC1"),
            _statement_of("AC2"),
        ]

    def test_the_statement_is_single_line_so_it_renders_foldproof(self) -> None:
        """``canonical_criterion_text`` collapses whitespace runs, and that matters.

        The contract renderer emits a long value as a literal block scalar, and
        that rendering assumes a single line. A multi-line statement would
        break the block and the OMN-15479 contamination ratchet would reject
        the file, so the property is pinned here rather than discovered there.
        """
        records = transcribe_ac_bindings(
            live_description=BODY,
            creation_revision=None,
            created_at=_CREATED_AT,
        )
        assert records
        for record in records:
            assert "\n" not in record.statement
            assert record.statement == record.statement.strip()


class TestNothingChangesForATicketWithNothingToSay:
    """The contract a ticket with no falsifiers mints must not move one byte."""

    def test_no_bindings_renders_no_requirements_key(self) -> None:
        rendered = _contract_with()
        assert "requirements:" not in rendered.replace("evidence_requirements:", "")
        contract = _parsed(rendered)
        assert "requirements" not in contract

    def test_no_bindings_is_byte_identical_to_the_unbound_shape(self) -> None:
        """A ticket that declared nothing gets the contract it always got.

        Guards the one regression this change could cause everywhere at once:
        an unconditional block would add a key to every companion the producer
        mints, including the overwhelming majority that carry no bindings.
        """
        without = _contract_with()
        assert "\nrequirements:\n" not in without
        assert without.startswith("---\nschema_version:")


class TestTheRenderedContractIsWellFormed:
    """Long prose has to survive the YAML layer and the formatter."""

    def test_a_long_statement_renders_as_a_literal_block_not_a_fold(self) -> None:
        """A folded or plain scalar is reflowed by yamlfmt, which corrupts it.

        The FRICTION recorded on OMN-19046 is explicit: a plain scalar gets the
        formatter's sentinel injected INTO the value and the OMN-15479 ratchet
        then rejects the file. Literal blocks are never refolded.
        """
        long_statement = _statement_of("AC1")
        assert len(long_statement) > 80, "fixture must exercise the long path"
        rendered = _contract_with(_record("AC1", long_statement))
        assert "statement: |-" in rendered
        # and it still parses back to exactly the text, chomped
        contract = _parsed(rendered)
        requirements = contract["requirements"]
        assert isinstance(requirements, list)
        first = requirements[0]
        assert isinstance(first, dict)
        acceptance = first["acceptance"]
        assert isinstance(acceptance, list)
        criterion = acceptance[0]
        assert isinstance(criterion, dict)
        assert criterion["statement"] == long_statement

    def test_both_producers_emit_the_identical_block(self) -> None:
        """OMN-18332's lesson, applied one layer up.

        That transcription landed on the compute producer, was not wired into
        the born producer, and seven of the next nine companions minted with
        no bindings at all. A model emitted by one producer and not the other
        reproduces that failure exactly, and a corpus census would show it
        only as "some contracts have requirements".

        Asserted as equality of the rendered block rather than presence on
        each side, so a second, differently-spelled renderer also fails here.
        """
        records = (
            _record("AC1", _statement_of("AC1")),
            _record("AC2", _statement_of("AC2")),
        )
        born = _parsed(_contract_with(*records))
        compute = _parsed(
            render_compute_companion_contract(
                ticket_id="OMN-19038",
                repo="OmniNode-ai/omnimarket",
                pr_number=2754,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-2754",
                ac_bindings=records,
            )
        )
        assert born["requirements"] == compute["requirements"]
        assert _acceptance_ids(compute) == ["AC1", "AC2"]
        assert set(_acceptance_ids(compute)) == set(_bound_labels(compute))

    def test_the_compute_producer_with_no_bindings_emits_no_key(self) -> None:
        rendered = render_compute_companion_contract(
            ticket_id="OMN-19038",
            repo="OmniNode-ai/omnimarket",
            pr_number=2754,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2754",
        )
        assert "\nrequirements:\n" not in rendered
        assert "requirements" not in _parsed(rendered)

    def test_the_block_renderer_is_empty_for_no_records(self) -> None:
        """The unit-level guard behind both byte-identity tests above."""
        assert render_requirements_block(()) == ""

    def test_requirements_precedes_dod_evidence_in_the_rendered_head(self) -> None:
        """Position is not cosmetic: the head template owns everything above it.

        ``dod_evidence`` is appended item by item after the head, so a
        ``requirements`` block emitted after it would land inside the evidence
        list rather than beside it.
        """
        rendered = _contract_with(_record("AC1", _statement_of("AC1")))
        assert rendered.index("\nrequirements:\n") < rendered.index("\ndod_evidence:\n")
