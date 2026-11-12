# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board's integration section from the integration projection (OMN-23899 AC5): golden and error board reads.

The golden read runs the whole chain the board runs: the pass's batch through the publisher's runtime, the floor's
projection reader writing the bundle into the registry home, and the floor cycle writing floor-simple.json. The error
reads show an absent, a stale and a malformed projection each drawn unread with its reason, never as an empty list,
and an overlay with no section block drawn not configured (the code tolerates the absent key before the overlays change).
"""

from __future__ import annotations

import asyncio
import copy
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.floor_projection import InMemorySnapshotSource
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.models.floor_17a.model_floor_integration import (
    ModelFloorIntegrationIn,
    ModelFloorIntegrationOverlay,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.integration_reading import ModelIntegrationReadingBatch
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    run as read_projections,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_integration import (
    build_integration_section,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_integration_reading_publish_effect import (
    runtime_integration_reading_publish as publish_runtime,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
    put_projection,
)
from tests.nodes.node_integration_projection_compute.test_integration_projection import (
    BATCH,
    GOLDEN_COUNTS,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.snapshot.projection.floor-integration.v1"
SECTION: dict[str, Any] = {
    "title": "Merged work that is not running yet",
    "group_titles": {
        "needs_fix": "Needs a fix",
        "waits_on_merge": "Waits on another change merging",
        "needs_install": "Needs an install",
        "needs_proof": "Needs one proving run",
        "needs_reading": "Needs a reading",
    },
    "max_rows": 40,
}


def _roots(tmp_path: Path, section: dict[str, Any] | None) -> Path:
    """The fixture overlays, with the floor overlay's integration block set (or left out)."""
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    path = roots / "node_floor_simple_compute" / "overlay.yaml"
    floor = yaml.safe_load(path.read_text(encoding="utf-8"))
    floor.pop("integration", None)
    if section is not None:
        floor["integration"] = section
    path.write_text(yaml.safe_dump(floor, sort_keys=False), encoding="utf-8")
    return roots


def _tick(home: Path, roots: Path) -> ModelFloorSimple:
    env = {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(roots)}
    result = HandlerFloorSimpleTick(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env
    )
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


def _publish_and_read(
    home: Path, as_of_shift: timedelta = timedelta(minutes=-10)
) -> None:
    """The pass publishes through the contract's runtime; the floor's reader writes the bundle into the home."""
    publisher = InMemoryEnvelopePublisher()
    batch = ModelIntegrationReadingBatch.model_validate(
        {**BATCH, "as_of": (NOW + as_of_shift).isoformat()}
    )
    asyncio.run(
        publish_runtime.run(
            batch,
            ModelFloorProjectionPublishOverlay(
                bus="inmemory", projection_id="integration"
            ),
            publisher,
            InMemoryEmissionSink(),
            producer="pass",
        )
    )
    overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 0.1,
            "projections": {"integration": {"topic": TOPIC, "max_age_s": 7800}},
        }
    )
    held = InMemorySnapshotSource(
        {TOPIC: [publisher.published[0].model_dump_json().encode()]}
    )
    asyncio.run(
        read_projections(home, overlay, held, InMemoryEmissionSink(), clock=lambda: NOW)
    )


def test_golden_board_read_the_pass_list_reaches_the_board_section(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    _publish_and_read(home)
    doc = _tick(home, _roots(tmp_path, SECTION))
    section = doc.integration
    assert section is not None
    assert section.status == "read"
    assert section.title == SECTION["title"]
    assert section.age_s == 600
    assert section.counts is not None
    assert section.counts.model_dump(mode="json") == GOLDEN_COUNTS
    assert [r.key for r in section.rows] == [
        "overlays#301",
        "omnimarket#5843",
        "omnimarket#5856",
    ]
    assert [(g.title, g.keys) for g in section.groups] == [
        ("Needs a fix", ("omnimarket#5856",)),
        ("Waits on another change merging", ("omnimarket#5843",)),
        ("Needs one proving run", ("overlays#301",)),
    ]
    assert section.rows_total == 3
    assert doc.sources_read["integration_projection"] is True


def test_error_board_read_an_absent_projection_draws_the_section_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    doc = _tick(home, _roots(tmp_path, SECTION))
    assert doc.integration is not None
    assert doc.integration.status == "unread"
    assert doc.integration.rows == ()
    assert doc.integration.counts is None
    assert doc.integration.reason
    assert doc.sources_read["integration_projection"] is False


def test_error_board_read_a_list_older_than_its_bound_is_unread_not_the_old_list(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    _publish_and_read(home, as_of_shift=timedelta(hours=-3))
    doc = _tick(home, _roots(tmp_path, SECTION))
    assert doc.integration is not None
    assert doc.integration.status == "unread"
    assert "over its 7800s bound" in doc.integration.reason
    assert doc.integration.rows == ()


def test_error_board_read_a_payload_that_is_not_an_integration_projection_is_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    put_projection(home, "integration", {"rows": "not rows"}, max_age_s=7800)
    doc = _tick(home, _roots(tmp_path, SECTION))
    assert doc.integration is not None
    assert doc.integration.status == "unread"
    assert "is not an integration projection" in doc.integration.reason


def test_pair_rule_an_overlay_with_no_section_draws_not_configured_and_still_publishes(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    _publish_and_read(home)
    doc = _tick(home, _roots(tmp_path, None))
    assert doc.integration is not None
    assert doc.integration.status == "not_configured"
    assert doc.integration.rows == ()


def test_the_floor_overlay_model_accepts_the_section_block_and_refuses_an_untitled_class() -> (
    None
):
    floor = copy.deepcopy(overlay_text("node_floor_simple_compute"))
    floor.pop("integration", None)
    assert ModelFloorSimpleOverlay.model_validate(floor).integration is None
    loaded = ModelFloorSimpleOverlay.model_validate({**floor, "integration": SECTION})
    assert loaded.integration is not None
    missing = copy.deepcopy(SECTION)
    del missing["group_titles"]["needs_reading"]
    with pytest.raises(ValidationError, match="missing \\['needs_reading'\\]"):
        ModelFloorIntegrationOverlay.model_validate(missing)
    with pytest.raises(ValidationError):
        ModelFloorSimpleOverlay.model_validate(
            {**floor, "integration": {**SECTION, "surprise": 1}}
        )


def test_the_section_draws_at_most_max_rows_and_counts_the_rest() -> None:
    projection = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch.model_validate(BATCH)
    )
    section = build_integration_section(
        ModelFloorIntegrationOverlay.model_validate({**SECTION, "max_rows": 2}),
        ModelFloorIntegrationIn(
            status="read", as_of=projection.as_of, age_s=5, projection=projection
        ),
    )
    assert [r.key for r in section.rows] == ["overlays#301", "omnimarket#5843"]
    assert section.rows_total == 3


def test_the_page_draws_the_section_from_the_record() -> None:
    page = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
    ).read_text(encoding="utf-8")
    assert "integrationSection(doc.integration)" in page
    assert 's.status === "not_configured"' in page
