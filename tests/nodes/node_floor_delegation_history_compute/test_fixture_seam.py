# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the data lane (delegation-history.json) and the page lane that draws it.

``fixtures/delegation-history.fixture.json`` is the document the page is built against and never invents a field beyond. It is the
fold's own output for ``fixtures/request.fixture.json``, so a change to the schema model, to the fold or to the readings' shape turns
this test red on whichever side moved: the page's reader and the fold agree on the document only while they agree on this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.events.floor_delegation_history import (
    SCHEMA_ID,
    ModelDelegationHistory,
)
from omnimarket.events.floor_history_request import (
    ModelHistoryRequest,
)
from omnimarket.nodes.node_floor_delegation_history_compute.handlers.handler_floor_delegation_history import (
    HandlerFloorDelegationHistory,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
SECTIONS = {"models", "series", "experiments"}


def _document() -> dict:
    return json.loads((FIXTURES / "delegation-history.fixture.json").read_text())


def test_the_fixture_is_the_folds_output_for_the_request_fixture() -> None:
    req = ModelHistoryRequest.model_validate_json(
        (FIXTURES / "request.fixture.json").read_text()
    )
    built = HandlerFloorDelegationHistory().handle(req)
    assert built.model_dump(mode="json") == _document()


def test_the_fixture_validates_against_the_schema_model_and_fills_all_three_sections() -> (
    None
):
    doc = ModelDelegationHistory.model_validate(_document())
    assert doc.schema_id == SCHEMA_ID
    assert set(_document()) >= SECTIONS
    assert doc.models
    assert doc.series
    assert doc.experiments
    assert {m.engine_kind for m in doc.models} >= {"local", "GLM"}
    assert all(s.days and s.hours for s in doc.series)
    assert all(e.arms for e in doc.experiments)


def test_error_a_field_the_schema_does_not_declare_is_refused() -> None:
    doc = _document()
    doc["models"][0]["invented_field"] = 1
    with pytest.raises(ValueError, match="invented_field"):
        ModelDelegationHistory.model_validate(doc)


def test_error_a_renamed_field_fails_the_fixture_seam() -> None:
    doc = _document()
    doc["series"][0]["days"][0]["attempts_made"] = doc["series"][0]["days"][0].pop(
        "attempts"
    )
    with pytest.raises(ValueError, match="attempts"):
        ModelDelegationHistory.model_validate(doc)
