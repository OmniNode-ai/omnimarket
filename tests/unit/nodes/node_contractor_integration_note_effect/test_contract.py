# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract validation for node_contractor_integration_note_effect (OMN-17277).

The contract is the node's interface, so these assertions are about promises
the rest of the platform is allowed to rely on: the handler and models the
contract names actually import, the declared secret ref resolves through the
contract rather than a source literal, the topics are canonically shaped, and
the shipped roster overlay parses into the model the node consumes.

A contract that names a module nobody can import is the exact "wired by
convention" failure the topic-graph oracle was built for; here it is caught one
node earlier, at the node's own boundary.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_contractor_integration_note_effect.cli import load_roster
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelIntegrationNoteRequest,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.adapters import (
    CONTRACT_PATH,
    LINEAR_SECRET_NAME,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROSTER_PATH = _REPO_ROOT / "config" / "contractor_roster.yaml"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "contract.yaml must load as a mapping"
    return raw


def test_contract_path_resolves_inside_the_node_package() -> None:
    assert CONTRACT_PATH.name == "contract.yaml"
    assert CONTRACT_PATH.parent.name == "node_contractor_integration_note_effect"
    assert CONTRACT_PATH.is_file()


def test_declared_handler_is_importable_and_definition_b(
    contract: dict[str, Any],
) -> None:
    handler_block = contract["handler"]
    module = importlib.import_module(handler_block["module"])
    handler_class = getattr(module, handler_block["class"])

    annotations = handler_class.handle.__annotations__
    assert set(annotations) == {"request", "return"}, (
        "the canonical definition-B signature is handle(request: ModelX) -> ModelY; "
        "an envelope-in/handler-output-out shape is not canonical"
    )


def test_declared_models_are_importable(contract: dict[str, Any]) -> None:
    for block in ("input_model", "output_model"):
        declared = contract[block]
        module = importlib.import_module(declared["module"])
        assert hasattr(module, declared["name"]), (
            f"{block} names {declared['name']}, which {declared['module']} does not export"
        )


def test_handler_input_model_matches_the_declared_input_model(
    contract: dict[str, Any],
) -> None:
    declared = contract["input_model"]
    assert contract["handler"]["input_model"] == (
        f"{declared['module']}.{declared['name']}"
    )


def test_secret_is_declared_in_the_contract_not_in_source() -> None:
    assert contract_secret_ref(CONTRACT_PATH, LINEAR_SECRET_NAME) == "LINEAR_API_KEY"


def test_secret_block_marks_the_credential_required(contract: dict[str, Any]) -> None:
    assert contract["secrets"][LINEAR_SECRET_NAME]["required"] is True


def test_command_address_is_declared_and_canonically_shaped(
    contract: dict[str, Any],
) -> None:
    """An EFFECT node is command-addressable; its address must be canonical."""
    bus = contract["event_bus"]
    command_topic = "onex.cmd.omnimarket.contractor-integration-note.v1"
    assert bus["subscribe_topics"] == [command_topic]
    assert bus["publish_topics"] == [contract["terminal_event"]]
    assert contract["runtime_dispatch"]["command_topic"] == command_topic
    assert (
        contract["runtime_dispatch"]["terminal_events"]["success"]
        == contract["terminal_event"]
    )
    for topic in [*bus["subscribe_topics"], *bus["publish_topics"]]:
        assert topic.startswith("onex."), topic
        assert topic.endswith(".v1"), topic


def test_the_github_pr_merged_feed_is_deliberately_not_subscribed(
    contract: dict[str, Any],
) -> None:
    """The obvious producer is left unsubscribed on purpose.

    A bus-dispatched shape cannot answer the note's reachability field: that
    comes from `git tag --contains` against the merged repo, and the runtime
    holds no checkout of it — the workflow does. Subscribing anyway would
    manufacture the wired-looking-but-unwired path the topic-graph oracle exists
    to catch, so this asserts the absence rather than leaving it to drift back.
    """
    assert (
        "onex.evt.github.pr-merged.v1" not in contract["event_bus"]["subscribe_topics"]
    )


def test_terminal_event_is_declared_externally_consumed(
    contract: dict[str, Any],
) -> None:
    """Its consumers are the reconciliation pass and the operator, not a node."""
    assert contract["terminal_event"] in contract["externally_consumed_topics"]


def test_declared_handler_is_boot_resolvable(contract: dict[str, Any]) -> None:
    """The handler must be constructible from the boot resolver's providers.

    At boot the runtime walks handler_routing and asks ServiceHandlerResolver to
    instantiate each declared handler, with only ``event_bus`` / ``container`` /
    ``ownership_query`` available. A required, default-less parameter makes the
    handler unresolvable, and it is quarantined behind a boot warning nobody
    reads (OMN-13551). This asserts the property directly rather than trusting
    the repo-wide scan to notice later.
    """
    routing = contract["handler_routing"]["handlers"]
    assert len(routing) == 1
    declared = routing[0]["handler"]
    module = importlib.import_module(declared["module"])
    handler_class = getattr(module, declared["name"])

    injectable = {"self", "event_bus", "container", "ownership_query"}
    unresolvable = [
        name
        for name, param in inspect.signature(handler_class.__init__).parameters.items()
        if name not in injectable and param.default is inspect.Parameter.empty
    ]
    assert unresolvable == [], (
        f"{declared['name']} would quarantine at boot on {unresolvable}"
    )


def test_the_checkout_path_is_a_required_input(contract: dict[str, Any]) -> None:
    """Reachability is a fact about the request, not about the deployment.

    Carrying the checkout in the payload is what lets the same node answer for a
    workflow-triggered merge and for anything else that can supply a tree. A
    caller with no checkout fails validation instead of silently reading an
    empty tag list as "not released" and shipping a pin recipe for something
    already released.
    """
    assert contract["inputs"]["checkout_path"]["required"] is True
    assert "checkout_path" in ModelIntegrationNoteRequest.model_fields


def test_effect_node_declares_the_effects_runtime_profile(
    contract: dict[str, Any],
) -> None:
    descriptor = contract["descriptor"]
    assert descriptor["node_archetype"] == "effect"
    assert descriptor["purity"] == "effectful"
    assert "effects" in descriptor["runtime_profiles"]


def test_write_side_effect_and_duplicate_key_are_declared(
    contract: dict[str, Any],
) -> None:
    side_effects = contract["side_effects"]
    assert "linear_comment" in side_effects["writes"]
    assert side_effects["duplicate_key_fields"] == ["repo", "pr_number"]


def test_shipped_roster_overlay_parses_and_is_non_empty() -> None:
    roster = load_roster(_ROSTER_PATH)
    assert roster.contractors, (
        "the shipped roster must configure at least one recipient"
    )
    assert all(entry.linear_user_id for entry in roster.contractors)
    assert "{merge_sha}" in roster.default_pin_recipe.template


def test_no_contractor_identity_is_hardcoded_in_the_node_package() -> None:
    """Identity lives in the overlay; the node must carry none of it."""
    package_root = CONTRACT_PATH.parent
    shipped_ids = {
        entry.linear_user_id for entry in load_roster(_ROSTER_PATH).contractors
    }
    for source in package_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for user_id in shipped_ids:
            assert user_id not in text, (
                f"{source} hardcodes a contractor id; the roster overlay is the "
                "only place identity may live"
            )
