# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18356. A suffixed criterion label (AC2b, AC10a) binds, not UNLABELLED.

Found live transcribing OMN-18332 by lane omn18332-autobinder-transcribe: 6 of
its 12 criteria (AC2b/c/d/e/f/g) parsed unlabelled because the vendored
``_CRITERION_LABEL`` grammar (byte-identical to omniclaude's admission guard at
the time) required a word boundary directly after the ordinal digits, which a
suffix letter never satisfies. Skipping is the dangerous direction: a
transcribed binding never mints for a criterion the autobinder cannot see.

Two levels are proven here: the vendored parser directly (mirrors the
omniclaude-side fix, OMN-18356), and the full ``transcribe_ac_bindings``
pipeline, so a regression that only showed up at the seam between the two
would still be caught.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from omnimarket.occ_ac_transcription import transcribe_ac_bindings
from omnimarket.occ_contract_pin import contract_pin_hashes
from omnimarket.occ_creation_revision import ModelCreationRevision
from omnimarket.occ_criterion_units import DEFAULT_CRITERION_POLICY, criterion_units

pytestmark = pytest.mark.unit

#: `onex_change_control`'s ``ModelAcBinding._AC_LABEL_RE``, copied verbatim.
#: A record whose label this does not match is refused at the model and takes
#: every other binding in the same evidence item down with it.
#:
#: REVERSED by OMN-18356's consumer half: the model now carries the identical
#: optional single-letter suffix group `omnimarket.occ_contract_pin` was
#: bumped to (`PORTED_FROM_REVISION`). ``AC2``, ``AC2b`` and ``AC10a`` are all
#: accepted now; nothing in this fixture's corpus is refused any more.
_OCC_AC_LABEL_RE = re.compile(r"^(AC|DOD)[-_ .]?(\d+)([a-zA-Z]?)$", re.IGNORECASE)

_SUFFIXED_MD = (
    "## Acceptance criteria\n"
    "\n"
    "* **AC2** — the base criterion. — falsifier: uv run pytest a.py\n"
    "* **AC2b** — a distinct sibling, must not be merged into AC2 or dropped. "
    "— falsifier: uv run pytest b.py\n"
    "* **AC10a** — a double-digit ordinal with a suffix. "
    "— falsifier: uv run pytest c.py\n"
)

_CREATED_AT = datetime(2026, 9, 14, 0, 0, 0, tzinfo=UTC)


class TestVendoredParserBindsASuffixedLabel:
    """Mirrors the omniclaude-side RED cases directly against the vendored copy."""

    def test_a_suffixed_ordinal_binds_as_its_own_labelled_unit(self) -> None:
        units = criterion_units(_SUFFIXED_MD, DEFAULT_CRITERION_POLICY)
        assert [unit.label for unit in units] == ["AC2", "AC2b", "AC10a"]
        # Never merged into the base ordinal's unit: three units, three hashes.
        assert len({unit.criterion_hash for unit in units}) == 3

    def test_a_suffixed_label_is_not_merged_into_its_base_ordinal_neighbour(
        self,
    ) -> None:
        units = criterion_units(_SUFFIXED_MD, DEFAULT_CRITERION_POLICY)
        base, suffixed, _ = units
        assert base.label == "AC2"
        assert suffixed.label == "AC2b"
        assert base.text != suffixed.text
        assert base.criterion_hash != suffixed.criterion_hash


class TestTranscriberNowBindsTheSuffixedLabelsToo:
    """Was ``TestTranscriberWithholdsALabelTheConsumerRefuses``, REVERSED by
    the consumer half of OMN-18356 exactly as that class's own docstring
    anticipated: "bumping PORTED_FROM_REVISION makes the withholding stop on
    its own, with no change here".

    `onex_change_control`'s ``ModelAcBinding`` now carries the identical
    optional single-letter suffix group `omnimarket.occ_contract_pin` was
    re-extracted to (`PORTED_FROM_REVISION` bumped to the fix's own squash).
    ``AC2b`` and ``AC10a`` join ``AC2`` as resolvable labels, so nothing in
    this fixture's corpus is refused at the model any more and the
    transcriber withholds nothing here. The withholding MECHANISM this class
    used to pin is unchanged -- ``transcribe_ac_bindings`` still calls
    :func:`omnimarket.occ_contract_pin.contract_pin_hashes` and still mints
    only what that function resolves -- what changed is what that function
    now resolves.
    """

    def _records(self) -> tuple[object, ...]:
        revision = ModelCreationRevision(
            snapshot_at=_CREATED_AT,
            content_data={"type": "doc", "content": []},
            actor_id="author-user-id",
        )
        return transcribe_ac_bindings(
            live_description=_SUFFIXED_MD,
            creation_revision=revision,
            created_at=_CREATED_AT,
            creator_id="author-user-id",
        )

    def test_the_parser_still_sees_every_suffixed_criterion(self) -> None:
        """The positive control. OMN-18356's own property, restated here.

        Without it, a regression that stopped the parser seeing ``AC2b`` would
        pass the binding assertion below for entirely the wrong reason.
        """
        units = criterion_units(_SUFFIXED_MD, DEFAULT_CRITERION_POLICY)

        assert [unit.label for unit in units] == ["AC2", "AC2b", "AC10a"]

    def test_every_label_the_consumer_now_resolves_reaches_the_contract(
        self,
    ) -> None:
        labels = sorted(record.label for record in self._records())

        assert labels == ["AC10a", "AC2", "AC2b"]
        assert set(contract_pin_hashes(_SUFFIXED_MD)) == {"AC10a", "AC2", "AC2b"}

    def test_nothing_is_withheld_because_nothing_is_refused(self) -> None:
        """Ties the (now-empty) withholding to the consumer's rule rather than
        to a list -- the same shape the predecessor test pinned, with the
        empty set as the reversal's own assertion."""
        minted = {record.label for record in self._records()}
        seen = {
            unit.label
            for unit in criterion_units(_SUFFIXED_MD, DEFAULT_CRITERION_POLICY)
            if unit.label is not None
        }

        assert seen - minted == set()
        for label in seen:
            assert _OCC_AC_LABEL_RE.match(label)
