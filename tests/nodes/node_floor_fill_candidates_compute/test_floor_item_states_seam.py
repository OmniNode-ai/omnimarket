# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the item states the factory floor compute publishes against the states the fill's candidates compute reads.

The publisher (``node_factory_floor_compute``, model ``ItemState``) writes each item's state into the floor record; the
reader (``node_floor_fill_candidates_compute``, model ``ModelRecordItem``) validates the whole record, so one state the
reader does not know makes the record FLOOR-UNREAD and the fill starts nothing. The two sets must be equal: a state added
on either side without the other reads ``broken`` here.

The seam is a published JSON document, not a topic; the bus legs of the same reader are in
``test_floor_fill_candidates_parity.py`` (the ``integrating`` scenarios), which runs on the in-memory bus everywhere and on
the Redpanda broker ``FLOOR_FILL_PARITY_BOOTSTRAP`` names.
"""

from __future__ import annotations

from typing import get_args

import pytest

from omnimarket.models.floor_fill.model_floor_fill_candidates import ModelRecordItem
from omnimarket.models.model_factory_floor_input import ItemState

pytestmark = pytest.mark.unit


def read_states() -> set[str]:
    return set(get_args(ModelRecordItem.model_fields["state"].annotation))


def seam_reads_passed(published: set[str], read: set[str]) -> bool:
    return published == read


def test_golden_the_reader_knows_every_state_the_publisher_declares() -> None:
    assert seam_reads_passed(set(get_args(ItemState)), read_states())


def test_error_chain_a_state_planted_on_the_publisher_side_reads_broken() -> None:
    assert not seam_reads_passed(set(get_args(ItemState)) | {"planted"}, read_states())


def test_error_chain_a_state_planted_on_the_reader_side_reads_broken() -> None:
    assert not seam_reads_passed(set(get_args(ItemState)), read_states() | {"planted"})
