# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-path cell (OMN-24535): a seen-running reading names its run.

A reading stamped running from an image revision, a merge or one ready answer has not
been seen running: it names the entry point that runs the change and one line from that
entry point's latest run. Every other state keeps its cells unchecked and may carry
them empty. The failing control is the real case omnimarket#6084, which the pass
stamped running from an image revision plus one readiness answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.integration_reading import (
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)

pytestmark = pytest.mark.unit

READ_AT = "2026-11-14T19:35:00Z"
ORIGIN = "integration-pass:orchestrator-31d9"
LINE = "2026-11-14T19:30Z node_x_effect tick refused 3 rows"


def reading(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "a change",
        "target": "the dev runtime",
        "read_at": READ_AT,
        "origin": ORIGIN,
    }
    return {**base, **fields}


def running_reading(cells: dict[str, Any]) -> dict[str, Any]:
    """One change read running; the caller sets the running cells it wants tried."""
    return reading(
        repository="omnimarket",
        number=6101,
        merged_at="2026-11-11T21:31:00Z",
        state="running",
        gap_class="none",
        **cells,
    )


def project(readings: list[dict[str, Any]]) -> dict[str, Any]:
    result = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch.model_validate(
            {"as_of": READ_AT, "origin": ORIGIN, "readings": readings}
        )
    )
    return result.model_dump(mode="json")


def test_golden_a_running_reading_with_both_cells_projects_seen_running() -> None:
    projection = project(
        [running_reading({"running_path": "node_x_effect", "running_line": LINE})]
    )
    assert projection["counts"]["merged"] == 1
    assert projection["counts"]["seen_running"] == 1
    row = projection["rows"]["omnimarket#6101"]
    assert row["running_path"] == "node_x_effect"
    assert row["running_line"] == LINE


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        ({}, "OMN-24535"),
        ({"running_path": "node_x_effect"}, "OMN-24535"),
        ({"running_line": LINE}, "OMN-24535"),
        ({"running_path": "none", "running_line": LINE}, "OMN-24535"),
        ({"running_path": "two words", "running_line": LINE}, "OMN-24535"),
        ({"running_path": "node_x_effect", "running_line": "tbd"}, "OMN-24535"),
        (
            {"running_path": "node_x_effect", "running_line": "node_x_effect"},
            "OMN-24535",
        ),
    ],
    ids=[
        "no-cells",
        "path-only",
        "line-only",
        "path-is-none",
        "path-is-two-words",
        "line-is-tbd",
        "line-is-the-path",
    ],
)
def test_error_chain_a_running_reading_without_its_run_is_refused_typed(
    cells: dict[str, Any], message: str
) -> None:
    bad = running_reading(cells)
    with pytest.raises(ValidationError, match=message):
        ModelIntegrationReading.model_validate(bad)
    with pytest.raises(ValidationError, match=message):
        project([bad])


@pytest.mark.parametrize(
    "cells_free",
    [
        {
            "state": "not_running",
            "gap_class": "needs_unit",
            "gap": "no unit for it on the Mac or the lab hosts",
            "step": "install a timer unit, read one clean run",
        },
        {"state": "ci_only", "gap_class": "none"},
        {"state": "library", "gap_class": "none"},
        {
            "state": "unproven",
            "gap_class": "needs_reading",
            "step": "one text step through the dev lane answered by a GPU",
        },
    ],
    ids=["not-running", "ci-only", "library", "unproven"],
)
def test_every_other_state_is_accepted_with_no_running_cells(
    cells_free: dict[str, Any],
) -> None:
    row = ModelIntegrationReading.model_validate(
        reading(
            repository="omnimarket",
            number=6102,
            merged_at="2026-11-11T21:31:00Z",
            **cells_free,
        )
    )
    assert row.state.value == cells_free["state"]


def test_control_omnimarket_6084_an_image_revision_is_not_a_run() -> None:
    stamped = reading(
        repository="omnimarket",
        number=6084,
        merged_at="2026-11-10T08:04:00Z",
        target="dev runtime",
        state="running",
        gap_class="none",
    )
    with pytest.raises(ValidationError, match="OMN-24535"):
        ModelIntegrationReading.model_validate(stamped)
    corrected = {
        **stamped,
        "running_path": "node_dev_deploy_reading_effect",
        "running_line": (
            "dev runtime answered revision 6084 and the node under change dispatched "
            "one command"
        ),
    }
    twin = ModelIntegrationReading.model_validate(corrected)
    assert twin.state.value == "running"
    assert twin.running_path == "node_dev_deploy_reading_effect"
