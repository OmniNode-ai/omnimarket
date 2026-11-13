# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the skill hook's record to the skill lifecycle fold.

    pre_tool_use_skill_started.sh builds {run_id, skill_name, repo_id, correlation_id}
      -> node_event_emit_effect enrichment adds emitted_at (and the rest of the envelope)
      -> onex.evt.omniclaude.skill-started.v1
      -> node_projection_skill_lifecycle fold

The hook keys a skill invocation by Claude's own tool_use_id, an opaque ``toolu_``
string, and keys correlation by the session id. The table's ``run_id`` and
``correlation_id`` columns are UUIDs. Until this seam was read, the fold refused
every hook record ("a required field is not of its declared type"), the writer
consumed events and upserted zero rows, and the dev runtime reported
``projection_apply_divergence`` and went unhealthy (OMN-19998, 2026-11-13).

The test builds the hook's record exactly as the hook builds it, runs it through the
emit node's own enrichment, and folds it. It fails if either side changes shape: the
hook's keys (the pinned record below), the enrichment's envelope, or the fold's
handling of an opaque run id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.enrichment import inject_metadata
from omnimarket.nodes.node_projection_skill_lifecycle.handlers import (
    HandlerProjectionSkillLifecycle,
)
from omnimarket.nodes.node_projection_skill_lifecycle.models import (
    ModelSkillLifecycleProjectionRequest,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle"
)
_STARTED_TOPIC, _COMPLETED_TOPIC = yaml.safe_load(
    (_NODE / "contract.yaml").read_text()
)["event_bus"]["subscribe_topics"]

# The hook's own values: Claude's tool_use_id and session id.
_TOOL_USE_ID = "toolu_01A09q90qw90lq917835lq9"
_SESSION_ID = "0b7f1a52-7a5e-4e53-b1b1-5c0a6a9f2c02"
_NOW = datetime(2026, 11, 13, 21, 0, 0, tzinfo=UTC)


def _hook_started(run_id: str = _TOOL_USE_ID) -> dict[str, Any]:
    """The exact keys pre_tool_use_skill_started.sh puts in its jq record."""
    return {
        "run_id": run_id,
        "skill_name": "preflight",
        "repo_id": "omniclaude",
        "correlation_id": _SESSION_ID,
    }


def _hook_completed(run_id: str = _TOOL_USE_ID) -> dict[str, Any]:
    return _hook_started(run_id) | {"status": "success"}


def _on_the_wire(record: dict[str, Any]) -> dict[str, Any]:
    return inject_metadata(record, clock=lambda: _NOW, env={})


def _fold(topic: str, record: dict[str, Any]) -> Any:
    return HandlerProjectionSkillLifecycle().handle(
        ModelSkillLifecycleProjectionRequest(topic=topic, payload=_on_the_wire(record))
    )


def test_a_hook_started_record_folds_to_a_row() -> None:
    result = _fold(_STARTED_TOPIC, _hook_started())
    assert result.skipped_reason is None
    assert result.row is not None
    assert result.row.skill_name == "preflight"
    assert result.row.correlation_id == UUID(_SESSION_ID)


def test_started_and_completed_hook_records_share_one_run_id() -> None:
    started = _fold(_STARTED_TOPIC, _hook_started())
    completed = _fold(_COMPLETED_TOPIC, _hook_completed())
    assert started.row is not None
    assert completed.row is not None
    assert started.row.run_id == completed.row.run_id


def test_the_same_tool_use_id_folds_to_the_same_run_id_on_every_replay() -> None:
    first = _fold(_STARTED_TOPIC, _hook_started())
    second = _fold(_STARTED_TOPIC, _hook_started())
    assert first.row is not None
    assert second.row is not None
    assert first.row.run_id == second.row.run_id
    assert first.row.event_id == second.row.event_id


def test_two_tool_use_ids_fold_to_two_run_ids() -> None:
    one = _fold(_STARTED_TOPIC, _hook_started("toolu_one"))
    other = _fold(_STARTED_TOPIC, _hook_started("toolu_other"))
    assert one.row is not None
    assert other.row is not None
    assert one.row.run_id != other.row.run_id


def test_a_uuid_run_id_is_stored_as_sent() -> None:
    sent = "6f1d9d3e-3a53-4d3b-8f0b-0f1c1e6f1a01"
    result = _fold(_STARTED_TOPIC, _hook_started(sent))
    assert result.row is not None
    assert result.row.run_id == UUID(sent)


@pytest.mark.parametrize("run_id", [12345, "", True, ["toolu_1"]])
def test_a_run_id_that_is_not_text_writes_nothing(run_id: object) -> None:
    result = _fold(_STARTED_TOPIC, _hook_started() | {"run_id": run_id})
    assert result.row is None
    assert result.skipped_reason is not None
