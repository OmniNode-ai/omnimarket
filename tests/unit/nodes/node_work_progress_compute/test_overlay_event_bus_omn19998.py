# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three node overlay documents carry the optional ``event_bus`` key (OMN-19998).

``onex run-node`` reads ``event_bus`` from the node's overlay document, exactly as
OMN-24088 did for node_github_repo_write_effect. Each of the three models must accept
the optional key while keeping ``extra="forbid"`` on every other key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.model_work_progress import ModelWorkProgressBudgets
from omnimarket.models.running_revision.model_running_revision_policy import (
    ModelRunningRevisionPolicy,
)
from omnimarket.nodes.node_work_progress_alert_effect.models.model_work_progress_alert_settings import (
    ModelWorkProgressAlertSettings,
)

pytestmark = pytest.mark.unit

_COMPUTE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_progress_compute"
)
_ALERT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_progress_alert_effect"
)

_BUDGETS_REQUIRED = {
    "budget_seconds": {"placed": 600, "claimed": 1800, "pr_open": 3600}
}
_ALERT_REQUIRED = {
    "ignore": {"lanes": [], "actors": []},
    "lookback_seconds": 86400,
    "check_interval_seconds": 300,
}
_POLICY_REQUIRED = {
    "known_repositories": ["omnimarket"],
    "behind_threshold_seconds": 3600,
    "median_merge_to_running_bound_seconds": 3600,
}


def _document(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# --- ModelWorkProgressBudgets ---


def test_budgets_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_BUDGETS_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelWorkProgressBudgets.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_budgets_shipped_overlay_example_loads() -> None:
    document = yaml.safe_load((_COMPUTE / "overlay.example.yaml").read_text("utf-8"))
    model = ModelWorkProgressBudgets.model_validate(document)
    assert model.event_bus is not None


def test_budgets_event_bus_kafka_without_lane_is_refused(tmp_path: Path) -> None:
    document = {**_BUDGETS_REQUIRED, "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelWorkProgressBudgets.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_budgets_unknown_extra_key_is_still_refused(tmp_path: Path) -> None:
    document = {**_BUDGETS_REQUIRED, "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelWorkProgressBudgets.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_budgets_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _BUDGETS_REQUIRED)
    model = ModelWorkProgressBudgets.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_model_accepts_event_bus_key_failing_control_budgets(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_BUDGETS_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelWorkProgressBudgets.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


# --- ModelWorkProgressAlertSettings ---


def test_alert_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_ALERT_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelWorkProgressAlertSettings.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_alert_shipped_overlay_example_loads() -> None:
    document = yaml.safe_load((_ALERT / "overlay.example.yaml").read_text("utf-8"))
    model = ModelWorkProgressAlertSettings.model_validate(document)
    assert model.event_bus is not None


def test_alert_event_bus_kafka_without_lane_is_refused(tmp_path: Path) -> None:
    document = {**_ALERT_REQUIRED, "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelWorkProgressAlertSettings.model_validate(
            yaml.safe_load(path.read_text("utf-8"))
        )


def test_alert_unknown_extra_key_is_still_refused(tmp_path: Path) -> None:
    document = {**_ALERT_REQUIRED, "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelWorkProgressAlertSettings.model_validate(
            yaml.safe_load(path.read_text("utf-8"))
        )


def test_alert_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _ALERT_REQUIRED)
    model = ModelWorkProgressAlertSettings.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_model_accepts_event_bus_key_failing_control_alert(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_ALERT_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelWorkProgressAlertSettings.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


# --- ModelRunningRevisionPolicy ---


def test_policy_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_POLICY_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelRunningRevisionPolicy.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_policy_event_bus_kafka_without_lane_is_refused(tmp_path: Path) -> None:
    document = {**_POLICY_REQUIRED, "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelRunningRevisionPolicy.model_validate(
            yaml.safe_load(path.read_text("utf-8"))
        )


def test_policy_unknown_extra_key_is_still_refused(tmp_path: Path) -> None:
    document = {**_POLICY_REQUIRED, "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelRunningRevisionPolicy.model_validate(
            yaml.safe_load(path.read_text("utf-8"))
        )


def test_policy_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _POLICY_REQUIRED)
    model = ModelRunningRevisionPolicy.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_model_accepts_event_bus_key_failing_control_policy(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_POLICY_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelRunningRevisionPolicy.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


# --- the nodes' own loaders read the same document the bus overlay reader reads ---


def _root_with(tmp_path: Path, node: str, document: dict) -> dict[str, str]:
    directory = tmp_path / "root" / node
    directory.mkdir(parents=True)
    (directory / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "root")}


def test_the_nodes_own_loaders_accept_a_document_with_event_bus(tmp_path: Path) -> None:
    from omnimarket.nodes.node_dev_running_revision_compute.overlay import (
        load_dev_running_revision_policy,
    )
    from omnimarket.nodes.node_work_progress_alert_effect.overlay import load_settings
    from omnimarket.nodes.node_work_progress_compute.overlay import load_budgets

    bus = {"type": "kafka", "lane": "dev"}
    budgets_env = _root_with(
        tmp_path / "a",
        "node_work_progress_compute",
        {**_BUDGETS_REQUIRED, "event_bus": bus},
    )
    assert load_budgets(budgets_env) == _BUDGETS_REQUIRED["budget_seconds"]

    alert_env = _root_with(
        tmp_path / "b",
        "node_work_progress_alert_effect",
        {**_ALERT_REQUIRED, "event_bus": bus},
    )
    assert load_settings(alert_env).check_interval_seconds == 300

    policy_env = _root_with(
        tmp_path / "c",
        "node_dev_running_revision_compute",
        {**_POLICY_REQUIRED, "event_bus": bus},
    )
    assert load_dev_running_revision_policy(policy_env).known_repositories == (
        "omnimarket",
    )


def test_the_loader_still_refuses_a_malformed_event_bus(tmp_path: Path) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_work_progress_compute.overlay import load_budgets

    env = _root_with(
        tmp_path,
        "node_work_progress_compute",
        {**_BUDGETS_REQUIRED, "event_bus": {"type": "kafka"}},
    )
    with pytest.raises(NodeOverlayError):
        load_budgets(env)
