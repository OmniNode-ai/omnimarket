# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of the decision-request policy the overlay must supply.

Every bound is an overlay value: there is no packaged fallback. The policy is
read the way the node reads it in production, from
``<root>/node_dev_roll_decision_request_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``, and a missing, incomplete or out-of-bound
document is refused.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestPolicy,
    ModelDevRollFileObservationStore,
    ModelDevRollInMemoryObservationStore,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.overlay import (
    NODE_NAME,
    load_decision_request_policy,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    OVERLAY_DOC,
    POLICY,
    SHADOW_ROLL_SECONDS,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
EXAMPLE_OVERLAY = (
    ROOT
    / "src/omnimarket/nodes/node_dev_roll_decision_request_effect/overlay.example.yaml"
)

REQUIRED_KEYS = (
    "mode",
    "issuer_repository_pattern",
    "shadow_roll_duration_seconds",
    "seen_job_ids_kept",
    "store",
)


def write_overlay(root: Path, doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Write ``<root>/<node>/overlay.yaml`` and return the environ that points at it."""
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_the_overlay_doc_loads_as_the_decision_request_policy(tmp_path: Path) -> None:
    policy = load_decision_request_policy(write_overlay(tmp_path))
    assert policy.mode == "shadow"
    assert policy.issuer_repository_pattern == POLICY.issuer_repository_pattern
    assert policy.shadow_roll_duration_seconds == SHADOW_ROLL_SECONDS
    assert policy.seen_job_ids_kept == 50
    store = policy.store
    assert isinstance(store, ModelDevRollFileObservationStore)
    assert store.home_env == "EXAMPLE_STATE_DIR"
    assert store.relpath == "dev-roll-decision-request"


def test_the_in_memory_store_choice_loads_too(tmp_path: Path) -> None:
    doc = {**OVERLAY_DOC, "store": {"backend": "in_memory"}}
    policy = load_decision_request_policy(write_overlay(tmp_path, doc))
    assert isinstance(policy.store, ModelDevRollInMemoryObservationStore)


def test_with_no_overlay_present_the_loader_refuses(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(NodeOverlayError):
        load_decision_request_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(root)})


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_a_required_key_removed_in_turn_is_refused(tmp_path: Path, key: str) -> None:
    doc = {name: value for name, value in OVERLAY_DOC.items() if name != key}
    with pytest.raises(NodeOverlayError):
        load_decision_request_policy(write_overlay(tmp_path, doc))


def test_a_mode_other_than_shadow_is_refused(tmp_path: Path) -> None:
    doc = {**OVERLAY_DOC, "mode": "live"}
    with pytest.raises(NodeOverlayError):
        load_decision_request_policy(write_overlay(tmp_path, doc))


def test_a_pattern_without_a_named_repository_group_is_refused(tmp_path: Path) -> None:
    doc = {**OVERLAY_DOC, "issuer_repository_pattern": "^gha/[a-z0-9_]+/pr-[0-9]+$"}
    with pytest.raises(NodeOverlayError):
        load_decision_request_policy(write_overlay(tmp_path, doc))


def test_a_pattern_that_does_not_compile_is_refused(tmp_path: Path) -> None:
    doc = {
        **OVERLAY_DOC,
        "issuer_repository_pattern": "^gha/(?P<repository>[a-z0-9_]+/pr-[0-9]+$",
    }
    with pytest.raises(NodeOverlayError):
        load_decision_request_policy(write_overlay(tmp_path, doc))


def test_the_shipped_example_overlay_loads_through_the_policy_model() -> None:
    doc = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    policy = ModelDevRollDecisionRequestPolicy.model_validate(doc)
    assert policy.mode == "shadow"
    assert "repository" in re.compile(policy.issuer_repository_pattern).groupindex
    assert policy.shadow_roll_duration_seconds == 300
    assert policy.seen_job_ids_kept == 200
    assert isinstance(policy.store, ModelDevRollInMemoryObservationStore)


def test_failing_control_an_unvalidated_stand_in_accepts_what_the_model_refuses() -> (
    None
):
    """The refusals above are real: a stand-in built without validation accepts the same out-of-bound document."""
    doc = {
        **OVERLAY_DOC,
        "mode": "live",
        "issuer_repository_pattern": "^gha/[a-z0-9_]+/pr-[0-9]+$",
    }
    stand_in = ModelDevRollDecisionRequestPolicy.model_construct(
        **{name: value for name, value in doc.items() if name != "store"},
        store=ModelDevRollFileObservationStore.model_construct(**doc["store"]),
    )
    assert stand_in.mode == "live"
    assert stand_in.issuer_repository_pattern == "^gha/[a-z0-9_]+/pr-[0-9]+$"
