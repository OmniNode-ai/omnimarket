# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18332. A minted binding is one the OCC gate can actually accept.

The autobinder wrote ``ac_bindings`` that `onex_change_control` refused on two
INDEPENDENT fields, so either defect alone made every machine-proposed binding
unusable and every companion for a falsifier-declaring ticket was refused
wholesale. Both are reproduced here against the real bytes of OCC#9486 -- the
companion ``node_occ_companion_compute`` minted for OMN-18358 at
2026-09-14T08:02:16Z -- rather than against a hand-built example, because a
shape proven against a hand-built example only proves the example.

* :class:`TestTheAcceptanceTimestampIsSecondPrecision` -- the producer rendered
  the ticket's Linear ``createdAt`` through ``datetime.isoformat()``, which
  emits six digits of microseconds when they are non-zero. OCC's
  ``ModelAcBinding`` requires RFC 3339 UTC to the SECOND, so all six entries
  were rejected and the whole evidence item failed as
  ``INVALID_DOD_EVIDENCE_ITEM``.
* :class:`TestThePinnedHashIsTheConsumersHash` -- the producer pinned the digest
  of the inline-markup-stripped comparison projection while the consumer hashes
  the raw markdown, so every pin was a total mismatch and the binding gate read
  them as ``ac_binding_stale_hash``.

The validator's regex is NEVER loosened to accommodate a producer. The producer
is what changes; the six expected digests below are the consumer's own, computed
by `onex_change_control`'s ``criterion_hash`` over the live OMN-18358 body.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_ac_bindings_block,
    render_accepted_ac_binding,
)
from omnimarket.occ_ac_transcription import (
    AUTOBINDER_IDENTITY,
    transcribe_ac_bindings,
)
from omnimarket.occ_contract_pin import contract_pin_hashes
from omnimarket.occ_creation_revision import ModelCreationRevision
from omnimarket.occ_criterion_normalizer import markdown_comparison_text
from omnimarket.occ_criterion_units import DEFAULT_CRITERION_POLICY, criterion_units

pytestmark = pytest.mark.unit

#: OCC `ModelAcBinding._UTC_TIMESTAMP_RE`, the shape the acceptance must render
#: in. Copied rather than imported for the reason the port module records: the
#: package is not in this repository's dependency set at all.
_OCC_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: OMN-18358's own creation instant, microseconds and all, exactly as Linear
#: reported it and exactly as the producer stamped it into OCC#9486.
_OMN_18358_CREATED_AT = datetime(2026, 9, 14, 7, 38, 18, 998000, tzinfo=UTC)

#: The ticket's creator. In the live contract this reached `accepted_by`.
_OMN_18358_AUTHOR = "7a850ce1-f95e-431f-b4e3-62f7449f04c0"

_OMN_18358_BODY = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ"
    / "omn_18358_live_description.md"
).read_text(encoding="utf-8")

#: What the OCC gate recomputes for OMN-18358's six criteria. Derived by running
#: `onex_change_control`'s own `criterion_hash` over `criteria_by_label` of the
#: body above; the clone-present leg of the sibling drift test re-derives them
#: from a live clone so a copying mistake here cannot survive.
_CONSUMER_DIGESTS = {
    "AC1": "66c91506a3806e4244f3c1ba220b6a251edb8d5a8b643b7d4f722d904c20942c",
    "AC2": "d4c7e02996f22c8a6d3e755b6c5d5824bb561737496d011f2e88c3b93323cb1d",
    "AC3": "4712644f426f98ad8a0d0d8b5e9371ffaab4062baedb29e69a08d42643e8deaa",
    "AC4": "81c5a74975aab9cd84013e209e4300ed65d1057d9983185a52aa64b08e4e3891",
    "AC5": "ff601c1f42594408ab4fe50319e9e2ba9207653ab54f6e9c9816ecf1f34f8924",
    "AC6": "b8e1ba73d4e830f87d9c70b52acaaf2fe52d004fd270df98013e9d451f01f869",
}

#: The digests the live producer actually wrote into OCC#9486, read from the
#: contract at commit 2a48c16b6cfa. They are the projection-space digests, and
#: the negative control below asserts the producer no longer emits them.
_PROJECTION_DIGESTS = {
    "AC1": "a39bdccbdc306dabbbdafd322d80b295373eb21faf171364d9800ec066c5b5e3",
    "AC2": "cd4392eb8aa175cc562dc81151564eabba9a729c4c2c4e8bca7579bc3c388a2e",
    "AC3": "0f9bf393698cef4274fe67591b6ae5ceea60da3571b2d9f04d116c0a909fa4f4",
    "AC4": "288c73ff834b7e3c7932a8a3d42d0c9f977767f0ee3b5c3a27a87bc0fd5aa0c4",
    "AC5": "42adf1ea8c6955ffcc9a8b1d58c797267ad62aedb5876e6cb1e4b9b17cc2382d",
    "AC6": "032a70023e9fb625c24c99701c58c3f81928d7b08fbde085d3924c259690ce63",
}


def _creation_revision(description: str) -> ModelCreationRevision:
    """A creation revision whose text equals ``description``'s.

    Built from the description's own projection so the transcriber's
    changed-since-creation comparison resolves to UNCHANGED and every record
    carries a full acceptance. That is the branch both defects lived on: a draft
    renders no timestamp at all, so a test that produced drafts would pass
    against the broken renderer.
    """
    blocks = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in markdown_comparison_text(description).splitlines()
    ]
    return ModelCreationRevision(
        snapshot_at=_OMN_18358_CREATED_AT,
        content_data={"type": "doc", "content": blocks},
        actor_id=_OMN_18358_AUTHOR,
    )


def _records(description: str = _OMN_18358_BODY) -> dict[str, Any]:
    records = transcribe_ac_bindings(
        live_description=description,
        creation_revision=_creation_revision(description),
        created_at=_OMN_18358_CREATED_AT,
        creator_id=_OMN_18358_AUTHOR,
    )
    return {record.label: record for record in records}


# --------------------------------------------- defect 1: the timestamp -------


class TestTheAcceptanceTimestampIsSecondPrecision:
    """`ModelAcBinding` demands RFC 3339 UTC to the second, with no fraction."""

    def test_a_microsecond_bearing_instant_renders_to_the_second(self) -> None:
        rendered = render_accepted_ac_binding(
            label="AC1",
            criterion_hash=_CONSUMER_DIGESTS["AC1"],
            proposed_by=AUTOBINDER_IDENTITY,
            accepted_by=_OMN_18358_AUTHOR,
            accepted_at=_OMN_18358_CREATED_AT,
        )

        assert 'accepted_at: "2026-09-14T07:38:18Z"' in rendered

    def test_every_rendered_acceptance_satisfies_the_validators_regex(self) -> None:
        block = render_ac_bindings_block(tuple(_records().values()))

        stamped = re.findall(r'accepted_at: "([^"]*)"', block)
        assert len(stamped) == len(_CONSUMER_DIGESTS)
        for value in stamped:
            assert _OCC_UTC_TIMESTAMP_RE.match(value), value

    def test_the_producers_original_spelling_is_a_negative_control(self) -> None:
        """The exact value OCC#9486 carried still fails the validator's regex."""
        assert not _OCC_UTC_TIMESTAMP_RE.match("2026-09-14T07:38:18.998000Z")

    def test_truncation_never_moves_the_instant_forward(self) -> None:
        rendered = render_accepted_ac_binding(
            label="AC1",
            criterion_hash=_CONSUMER_DIGESTS["AC1"],
            proposed_by=AUTOBINDER_IDENTITY,
            accepted_by=_OMN_18358_AUTHOR,
            accepted_at=datetime(2026, 9, 14, 7, 38, 18, 999999, tzinfo=UTC),
        )

        assert 'accepted_at: "2026-09-14T07:38:18Z"' in rendered


# --------------------------------------------------- defect 2: the hash ------


class TestThePinnedHashIsTheConsumersHash:
    """The pin is what the OCC gate recomputes, not the projection's digest."""

    @pytest.mark.parametrize("label", sorted(_CONSUMER_DIGESTS))
    def test_each_live_criterion_pins_the_consumers_digest(self, label: str) -> None:
        assert _records()[label].criterion_hash == _CONSUMER_DIGESTS[label]

    @pytest.mark.parametrize("label", sorted(_PROJECTION_DIGESTS))
    def test_no_record_carries_the_projection_digest(self, label: str) -> None:
        """Negative control: the six values OCC#9486 actually shipped."""
        assert _records()[label].criterion_hash != _PROJECTION_DIGESTS[label]

    def test_the_projection_is_still_what_decides_acceptance(self) -> None:
        """The projection keeps its own job: has this criterion MOVED?

        An edit to AC1's wording must demote AC1 to a draft and leave every
        other criterion accepted. Pinning in consumer space must not cost that.
        """
        edited = _OMN_18358_BODY.replace(
            "leaves the worktree, the index and HEAD byte-identical",
            "leaves the worktree and the index byte-identical",
        )
        assert edited != _OMN_18358_BODY

        records = transcribe_ac_bindings(
            live_description=edited,
            creation_revision=_creation_revision(_OMN_18358_BODY),
            created_at=_OMN_18358_CREATED_AT,
            creator_id=_OMN_18358_AUTHOR,
        )
        by_label = {record.label: record for record in records}

        assert by_label["AC1"].accepted_by is None
        assert by_label["AC1"].criterion_hash == contract_pin_hashes(edited)["AC1"]
        assert all(
            by_label[label].accepted_by == _OMN_18358_AUTHOR
            for label in ("AC2", "AC3", "AC4", "AC5", "AC6")
        )

    def test_a_wrapped_criterion_still_pins_what_the_consumer_will_compute(
        self,
    ) -> None:
        """The two readers disagree about where one criterion ENDS.

        The vendored guard parser joins an item's continuation lines; the
        consumer's reader takes one item per line and stops. The producer must
        still pin the digest the consumer will recompute, or a criterion whose
        falsifier is written on a wrapped line is refused as stale for a reason
        that has nothing to do with the criterion changing.
        """
        wrapped = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC1** the lane is green\n"
            "  \u2014 falsifier: run the suite\n"
        )
        producer_unit = next(
            unit
            for unit in criterion_units(
                markdown_comparison_text(wrapped), DEFAULT_CRITERION_POLICY
            )
            if unit.label == "AC1"
        )
        consumer_digest = contract_pin_hashes(wrapped)["AC1"]
        assert producer_unit.criterion_hash != consumer_digest

        assert _records(wrapped)["AC1"].criterion_hash == consumer_digest

    def test_a_label_the_consumer_cannot_resolve_yields_no_binding(self) -> None:
        """Fail closed: never pin a digest the gate has nothing to match.

        A zero-padded ordinal is the measured case. The guard's label rule
        keeps the digits as written, so the producer reads ``AC01``; the
        consumer's canonicaliser parses the ordinal as an integer and calls the
        same criterion ``AC1``. Minting ``AC01`` would be an
        ``ac_binding_unknown_criterion`` refusal, so nothing is minted.
        """
        padded = (
            "## Acceptance criteria\n"
            "\n"
            "* **AC01** the lane is green. \u2014 falsifier: run the suite\n"
        )
        producer_labels = {
            unit.label
            for unit in criterion_units(
                markdown_comparison_text(padded), DEFAULT_CRITERION_POLICY
            )
        }
        assert producer_labels == {"AC01"}
        assert set(contract_pin_hashes(padded)) == {"AC1"}

        assert _records(padded) == {}


# ------------------------------------------- the two defects, together -------


class TestTheMintedBlockIsAcceptable:
    """Both fields, in one rendered block, as the contract would carry it."""

    def test_the_block_carries_the_consumer_digests_at_second_precision(self) -> None:
        block = render_ac_bindings_block(tuple(_records().values()))

        for label, digest in _CONSUMER_DIGESTS.items():
            assert f'- label: "{label}"' in block
            assert f'criterion_hash: "{digest}"' in block
        assert ".998000Z" not in block
        assert block.count('accepted_at: "2026-09-14T07:38:18Z"') == 6
