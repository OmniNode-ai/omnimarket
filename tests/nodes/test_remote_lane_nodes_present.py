# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane migration's package, contract and deployment proof."""

import importlib
from dataclasses import is_dataclass
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = (
    "node_install_canary_effect",
    "node_codex_landing_compute",
    "node_brief_prerequisites_compute",
    "node_lane_pin_fallback_compute",
    "node_lane_queue_argv_compute",
    "node_dispatch_brief_shape_compute",
    "node_codex_gitlink_compute",
    "node_pr_head_stage_compute",
    "node_ticket_text_effect",
    "node_lane_github_budget_effect",
    "node_lane_report_relay_effect",
)
ROOT = Path(__file__).resolve().parents[2]


def node_files():
    return [
        path
        for name in NODES
        for path in (ROOT / "src/omnimarket/nodes" / name).rglob("*")
        if path.is_file() and path.suffix in {".py", ".yaml"}
    ]


def test_43_moved_files_plus_eleven_metadata_files():
    files = node_files()
    assert len(files) == 54
    assert sum(path.name == "metadata.yaml" for path in files) == 11
    for name in NODES:
        root = ROOT / "src/omnimarket/nodes" / name
        assert (root / "contract.yaml").is_file()
        assert (root / "__init__.py").is_file()
        assert len(list((root / "handlers").glob("handler_*.py"))) == 1


def test_moved_files_do_not_name_the_old_registry_variable():
    files = node_files()
    assert len(files) == 54
    legacy_name = "OMNI" + "_HOME"
    assert not [
        str(path.relative_to(ROOT)) for path in files if legacy_name in path.read_text()
    ]


@pytest.mark.parametrize("name", NODES)
def test_contract_models_are_frozen_and_handler_is_typed(name):
    contract = yaml.safe_load(
        (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").read_text()
    )
    for field in ("input_model", "output_model"):
        spec = contract[field]
        model = getattr(importlib.import_module(spec["module"]), spec["name"])
        assert is_dataclass(model)
        assert model.__dataclass_params__.frozen
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])
    assert handler.handle.__annotations__["request"]
    assert handler.handle.__annotations__["return"]


def test_all_eleven_installed_entry_points_load():
    installed = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    for name in NODES:
        module = installed[name].load()
        assert module.__name__ == "omnimarket.nodes." + name
        node = importlib.import_module(module.__name__)
        assert node is not None


@pytest.mark.parametrize("name", NODES)
def test_command_and_terminal_are_wired_to_the_handler(name):
    contract = yaml.safe_load(
        (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").read_text()
    )
    command = contract["runtime_dispatch"]["command_topic"]
    terminal = contract["terminal_event"]
    assert contract["event_bus"] == {
        "subscribe_topics": [command],
        "publish_topics": [terminal],
    }
    assert contract["externally_consumed_topics"] == [terminal]
    assert contract["metadata"]["transport_type"] == "inmemory"
    assert all(
        route["topic"] == command for route in contract["handler_routing"]["handlers"]
    )


@pytest.mark.parametrize(
    ("name", "terminal"),
    [
        (
            "node_brief_prerequisites_compute",
            "onex.evt.omnimarket.brief-prerequisites-completed.v1",
        ),
        (
            "node_codex_gitlink_compute",
            "onex.evt.omnimarket.codex-gitlink-completed.v1",
        ),
        (
            "node_codex_landing_compute",
            "onex.evt.omnimarket.codex-landing-completed.v1",
        ),
        (
            "node_dispatch_brief_shape_compute",
            "onex.evt.omnimarket.dispatch-brief-shape-completed.v1",
        ),
        (
            "node_install_canary_effect",
            "onex.evt.omnimarket.install-canary-completed.v1",
        ),
        (
            "node_lane_github_budget_effect",
            "onex.evt.omnimarket.lane-github-budget-completed.v1",
        ),
        (
            "node_lane_pin_fallback_compute",
            "onex.evt.omnimarket.lane-pin-fallback-completed.v1",
        ),
        (
            "node_lane_queue_argv_compute",
            "onex.evt.omnimarket.lane-queue-argv-completed.v1",
        ),
        (
            "node_lane_report_relay_effect",
            "onex.evt.omnimarket.lane-report-relay-completed.v1",
        ),
        (
            "node_pr_head_stage_compute",
            "onex.evt.omnimarket.pr-head-stage-completed.v1",
        ),
        ("node_ticket_text_effect", "onex.evt.omnimarket.ticket-text-completed.v1"),
    ],
)
def test_each_node_declares_its_terminal_and_runtime_profile(name, terminal):
    contract = yaml.safe_load(
        (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").read_text()
    )
    profile = "effects" if name.endswith("_effect") else "main"
    assert contract["terminal_event"] == terminal
    assert contract["runtime_profiles"] == [profile]
    assert contract["descriptor"]["runtime_profiles"] == [profile]
