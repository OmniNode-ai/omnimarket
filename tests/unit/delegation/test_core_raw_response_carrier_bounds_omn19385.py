# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19385: the Core raw-response carrier keeps its bound and refuses a forgery.

The delegation terminals of this repo carry ``ModelDelegationRawResponse`` from
omnibase_core. The hosted repo-evidence verifier checks the sibling omnibase_core
at the revision this repository's workflows pin, which predates the carrier, so
the Core repository's own test file is not reachable there. These tests run the
carrier's contract against the omnibase_core this repository installs, so the
hosted verifier can check the Core half of the retention without a sibling.
"""

from __future__ import annotations

import hashlib

import pytest
from omnibase_core.models.delegation.wire import (
    MAX_RAW_RESPONSE_UTF8_BYTES,
    ModelDelegationRawResponse,
)
from pydantic import ValidationError

_SOURCE = "choices[0].message.content"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_core_carrier_keeps_exact_text_sha256_and_utf8_byte_count() -> None:
    raw = '  {"score": 0.9}\n\nThat is my assessment. résumé ✓\n'

    carrier = ModelDelegationRawResponse.from_provider_content(
        raw, source_field=_SOURCE
    )

    assert carrier.text == raw
    assert carrier.sha256 == _sha(raw)
    assert carrier.utf8_bytes == len(raw.encode("utf-8"))
    assert carrier.utf8_bytes > len(raw)
    assert carrier.source_field == _SOURCE


@pytest.mark.unit
def test_core_carrier_at_the_bound_keeps_text_and_over_it_keeps_only_hash_and_length() -> (
    None
):
    at_bound = "y" * MAX_RAW_RESPONSE_UTF8_BYTES
    over_bound = "x" * (MAX_RAW_RESPONSE_UTF8_BYTES + 1)

    kept = ModelDelegationRawResponse.from_provider_content(
        at_bound, source_field=_SOURCE
    )
    dropped = ModelDelegationRawResponse.from_provider_content(
        over_bound, source_field=_SOURCE
    )

    assert kept.text == at_bound
    assert dropped.text is None
    assert dropped.sha256 == _sha(over_bound)
    assert dropped.utf8_bytes == MAX_RAW_RESPONSE_UTF8_BYTES + 1


@pytest.mark.unit
def test_core_carrier_refuses_text_that_does_not_hash_to_its_sha256() -> None:
    forged = "a forgery!!!"

    with pytest.raises(ValidationError, match="sha256"):
        ModelDelegationRawResponse(
            source_field=_SOURCE,
            sha256=_sha("the original"),
            utf8_bytes=len(forged.encode("utf-8")),
            text=forged,
        )


@pytest.mark.unit
def test_core_carrier_refuses_a_length_that_disagrees_with_the_text() -> None:
    raw = "abc"

    with pytest.raises(ValidationError, match="utf8_bytes"):
        ModelDelegationRawResponse(
            source_field=_SOURCE, sha256=_sha(raw), utf8_bytes=4, text=raw
        )


@pytest.mark.unit
def test_core_carrier_refuses_dropped_text_under_the_bound() -> None:
    with pytest.raises(ValidationError, match="bound"):
        ModelDelegationRawResponse(
            source_field=_SOURCE, sha256=_sha("abc"), utf8_bytes=3, text=None
        )
