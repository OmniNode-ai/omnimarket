# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.4 AC3 labels only rows carrying both typed linking cells as linked."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_context_source_ledger_effect.models import (
    ModelLedgerSourceEntry,
)
from tests.unit.nodes.node_context_source_ledger_effect.test_context_source_ledger import (
    _handler,
    _ListReader,
    _request,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ("prompt_id=p | covers=c |", "recorded, linked"),
        ("prompt_id=p |", "recorded, unlinked"),
        ("covers=c |", "recorded, unlinked"),
        ("prompt_id= | covers=c |", "recorded, unlinked"),
        ("prompt_id=p | covers= |", "recorded, unlinked"),
        ('"prompt_id=p covers=c" |', "recorded, unlinked"),
        ("", "recorded, unlinked"),
    ],
)
def test_provenance_label_requires_both_linking_cells(
    row_type: str,
    cells: str,
    expected: Literal["recorded, linked", "recorded, unlinked"],
) -> None:
    row = f'2026-10-02T11:25:17Z | {row_type} | lane=proof | ticket=OMN-21127 | kind=decision | {cells} "keep the words" | recorded'
    (entry,) = (
        _handler(_ListReader((row,))).handle(_request("2026-10-02T12:00:00Z")).entries
    )
    assert entry.provenance_label == expected
    assert entry.model_dump(mode="json")["provenance_label"] == expected
    assert entry.text == row
    forged = entry.model_dump()
    forged["provenance_label"] = (
        "recorded, unlinked" if expected == "recorded, linked" else "recorded, linked"
    )
    with pytest.raises(ValidationError, match="provenance_label"):
        ModelLedgerSourceEntry.model_validate(forged)
