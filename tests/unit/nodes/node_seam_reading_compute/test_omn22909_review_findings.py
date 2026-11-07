# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two review findings on the seam reading, each read against the code (OMN-22909).

* A row key was made from a whole-second stamp, so two readings of one seam inside the same
  second shared a key and the second was refused as a rewrite.
* A request could demand two different verdicts for one seam; the handler kept the last, so
  the order of the entries decided the overall reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_reading_compute.models.model_required_sender import (
    EnumSeamTrigger,
    ModelDemandedVerdict,
    ModelRequiredSenderRequest,
    ModelSeamDeclaration,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import row_key

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)


def test_two_readings_of_one_seam_in_one_second_have_different_keys() -> None:
    first = NOW + timedelta(milliseconds=100)
    second = NOW + timedelta(milliseconds=900)
    assert row_key("seam-a", first) != row_key("seam-a", second)


def test_the_same_instant_in_another_zone_keeps_one_key() -> None:
    plus_two = datetime(2026, 11, 7, 16, 0, 0, 100000, tzinfo=UTC).astimezone(
        datetime.now().astimezone().tzinfo
    )
    assert row_key("seam-a", plus_two) == row_key(
        "seam-a", NOW + timedelta(hours=2, milliseconds=100)
    )


def _request(*demanded: ModelDemandedVerdict) -> ModelRequiredSenderRequest:
    return ModelRequiredSenderRequest(
        declarations=(
            ModelSeamDeclaration(
                seam="seam-a",
                trigger=EnumSeamTrigger.ON_DEMAND,
                required=True,
                sender=None,
            ),
        ),
        demanded=demanded,
        now=NOW,
    )


@pytest.mark.parametrize(
    "order",
    [
        (EnumReadingVerdict.NOT_WORKING, EnumReadingVerdict.WORKING),
        (EnumReadingVerdict.WORKING, EnumReadingVerdict.NOT_WORKING),
    ],
)
def test_two_different_demanded_verdicts_for_one_seam_are_refused(
    order: tuple[EnumReadingVerdict, EnumReadingVerdict],
) -> None:
    with pytest.raises(ValidationError, match="demanded twice"):
        _request(*(ModelDemandedVerdict(seam="seam-a", verdict=v) for v in order))


def test_one_demanded_verdict_per_seam_is_still_accepted() -> None:
    request = _request(
        ModelDemandedVerdict(seam="seam-a", verdict=EnumReadingVerdict.WORKING)
    )
    assert len(request.demanded) == 1
