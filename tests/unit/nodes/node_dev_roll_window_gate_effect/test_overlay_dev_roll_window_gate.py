# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of the gate policy the overlay must supply.

Every value is an overlay value: there is no packaged fallback. The policy is read the way the node
reads it in production, from ``<root>/node_dev_roll_window_gate_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``; a missing, incomplete or out-of-bound document is refused, and the node's
source carries no bound, topic or repository name of its own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollPolicy
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.store_dev_roll_gate import (
    FileDevRollGateStore,
    InMemoryDevRollGateStore,
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateFileStore,
    ModelDevRollGateInMemoryStore,
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
    ModelHeldDevRoll,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.overlay import (
    NODE_NAME,
    load_gate_policy,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    NOW,
    OVERLAY_DOC,
    POLICY,
    WINDOW,
    command,
    sha,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_dev_roll_window_gate_effect"
EXAMPLE_OVERLAY = NODE_DIR / "overlay.example.yaml"

REQUIRED_KEYS = tuple(OVERLAY_DOC)


def write_overlay(root: Path, doc: dict[str, Any] | None = None) -> dict[str, str]:
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_the_overlay_doc_loads_as_the_gate_policy(tmp_path: Path) -> None:
    policy = load_gate_policy(write_overlay(tmp_path))
    assert policy.mode == "live"
    assert policy.batch_window_seconds == WINDOW
    assert policy.known_repositories == ("omnimarket", "omnibase_infra")
    store = policy.store
    assert isinstance(store, ModelDevRollGateFileStore)
    assert (store.home_env, store.relpath) == (
        "EXAMPLE_STATE_DIR",
        "dev-roll-window-gate",
    )


def test_the_policy_hands_the_batching_node_the_same_bounds() -> None:
    roll = POLICY.to_roll_policy()
    assert isinstance(roll, ModelDevRollPolicy)
    assert roll.batch_window_seconds == POLICY.batch_window_seconds
    assert roll.min_start_interval_seconds == POLICY.min_start_interval_seconds
    assert roll.healthy_for_seconds == POLICY.healthy_for_seconds
    assert roll.mode == "live"


def test_with_no_overlay_present_the_loader_refuses_by_name(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(NodeOverlayNotFoundError):
        load_gate_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(root)})


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_a_required_key_removed_in_turn_is_refused(tmp_path: Path, key: str) -> None:
    doc = {name: value for name, value in OVERLAY_DOC.items() if name != key}
    with pytest.raises(NodeOverlayError):
        load_gate_policy(write_overlay(tmp_path, doc))


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "shadow"},
        {"batch_window_seconds": 0},
        {"min_start_interval_seconds": -1},
        {"held_kept": 0},
        {"issuer_repository_pattern": "^gha/[a-z0-9_]+/pr-[0-9]+$"},
        {"issuer_repository_pattern": "^gha/(?P<repository>[a-z0-9_]+/pr$"},
        {"build_context_repository": "omniclaude"},
    ],
)
def test_an_out_of_bound_value_is_refused(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(NodeOverlayError):
        load_gate_policy(write_overlay(tmp_path, {**OVERLAY_DOC, **bad}))


def test_the_shipped_example_overlay_loads_through_the_policy_model() -> None:
    doc = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    policy = ModelDevRollWindowGatePolicy.model_validate(doc)
    assert policy.mode == "live"
    assert "repository" in re.compile(policy.issuer_repository_pattern).groupindex
    assert isinstance(policy.store, ModelDevRollGateInMemoryStore)


_NUMBER_LITERAL = re.compile(r"(?<![\w\[.\-:])[1-9][0-9]+\b")
_TOPIC_LITERAL = re.compile(r"onex\.(cmd|evt|intent)\.")


def source_offenders(text: str, name: str) -> list[str]:
    """Lines that carry a topic string or a two-digit-or-longer number outside a comment."""
    offenders: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if _TOPIC_LITERAL.search(code):
            offenders.append(f"{name}:{number} topic")
        if _NUMBER_LITERAL.search(code) and "{40}" not in code:
            offenders.append(f"{name}:{number} number")
    return offenders


def test_the_node_source_carries_no_bound_or_topic_of_its_own() -> None:
    """Every bound lives in the overlay and every topic in contract.yaml."""
    offenders: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        offenders += source_offenders(path.read_text(encoding="utf-8"), path.name)
    assert offenders == []


def test_failing_control_the_scan_catches_a_window_literal_and_a_topic_string() -> None:
    assert source_offenders("    window = 900\n", "x.py") == ["x.py:1 number"]
    assert source_offenders('    topic = "onex.cmd.x.v1"\n', "x.py") == ["x.py:1 topic"]


def test_the_file_store_keeps_state_across_instances(tmp_path: Path) -> None:
    policy = ModelDevRollWindowGatePolicy.model_validate(OVERLAY_DOC)
    environ = {"EXAMPLE_STATE_DIR": str(tmp_path)}
    first = store_from_policy(policy, environ)
    assert isinstance(first, FileDevRollGateStore)
    state = ModelDevRollGateState()
    one = command(number=3)
    first.save(state)
    held = state.model_copy(
        update={
            "held": (
                ModelHeldDevRoll(
                    command=one,
                    repository="omnimarket",
                    sha=sha(3),
                    held_at=NOW,
                ),
            )
        }
    )
    first.save(held)
    again = store_from_policy(policy, environ)
    assert again.load() == held


def test_a_corrupt_file_store_raises_and_never_reads_as_empty(tmp_path: Path) -> None:
    policy = ModelDevRollWindowGatePolicy.model_validate(OVERLAY_DOC)
    store = store_from_policy(policy, {"EXAMPLE_STATE_DIR": str(tmp_path)})
    store.save(ModelDevRollGateState())
    (tmp_path / "dev-roll-window-gate" / "gate-state.json").write_text("{not json")
    with pytest.raises(ValueError, match="unreadable"):
        store.load()


def test_the_in_memory_store_choice_builds_the_in_memory_store() -> None:
    assert isinstance(store_from_policy(POLICY, {}), InMemoryDevRollGateStore)


def test_the_file_store_choice_without_its_home_variable_refuses(
    tmp_path: Path,
) -> None:
    policy = ModelDevRollWindowGatePolicy.model_validate(OVERLAY_DOC)
    with pytest.raises(NodeOverlayError):
        store_from_policy(policy, {})
