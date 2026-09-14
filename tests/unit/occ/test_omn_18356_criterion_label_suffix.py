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

from datetime import UTC, datetime

import pytest

from omnimarket.occ_ac_transcription import transcribe_ac_bindings
from omnimarket.occ_creation_revision import ModelCreationRevision
from omnimarket.occ_criterion_units import DEFAULT_CRITERION_POLICY, criterion_units

pytestmark = pytest.mark.unit

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


class TestTranscriberDoesNotDropASuffixedCriterion:
    """End to end: the full pipeline must not skip a suffixed criterion either."""

    def test_all_three_criteria_reach_the_transcriber_output(self) -> None:
        revision = ModelCreationRevision(
            snapshot_at=_CREATED_AT,
            content_data={"type": "doc", "content": []},
            actor_id="author-user-id",
        )
        records = transcribe_ac_bindings(
            live_description=_SUFFIXED_MD,
            creation_revision=revision,
            created_at=_CREATED_AT,
            creator_id="author-user-id",
        )
        labels = sorted(record.label for record in records)
        assert labels == ["AC10a", "AC2", "AC2b"]
