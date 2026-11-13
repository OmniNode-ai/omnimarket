# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for node_ci_checkout_effect: pin the request/result shapes and topics."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    ci_group,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_git import (
    CommandCloneSync,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)

pytestmark = pytest.mark.unit


def test_request_result_json_field_sets() -> None:
    req_schema = ModelCiCheckoutRequest.model_json_schema()
    res_schema = ModelCiCheckoutResult.model_json_schema()
    req_fields = set(req_schema.get("properties", {}).keys())
    res_fields = set(res_schema.get("properties", {}).keys())
    assert req_fields == {
        "correlation_id",
        "repo",
        "sha",
        "ref",
        "requested_by_lane",
        "host",
    }
    assert res_fields == {
        "correlation_id",
        "host",
        "status",
        "repo",
        "sha",
        "source_name",
        "serve_url",
        "upstream_fetches",
        "detail",
    }


def test_command_and_terminal_topics() -> None:
    node = resources.files("omnimarket.nodes.node_ci_checkout_effect")
    contract = yaml.safe_load(node.joinpath("contract.yaml").read_text())
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"] == "onex.cmd.omnimarket.ci-checkout-requested.v1"
    assert dispatch["terminal_events"]["success"] == (
        "onex.evt.omnimarket.ci-checkout-completed.v1"
    )
    assert dispatch["terminal_events"]["failure"] == (
        "onex.evt.omnimarket.ci-checkout-failed.v1"
    )


ACTION = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "actions"
    / "bus-checkout"
    / "action.yml"
)
#: action input -> the CLI option it feeds. Spelled here, so a rename on either side fails this test.
ACTION_INPUT_TO_CLI_OPTION = {
    "repository": "--repo",
    "sha": "--sha",
    "path": "--dest",
    "ref": "--ref",
    "lane": "--lane",
    "host": "--host",
    "bus": "--bus",
    "bus-lane": "--bus-lane",
    "timeout-s": "--timeout-s",
}


def test_composite_action_inputs_feed_the_cli_options() -> None:
    action = yaml.safe_load(ACTION.read_text())
    assert set(action["inputs"]) == {*ACTION_INPUT_TO_CLI_OPTION, "onex-bin"}
    script = action["runs"]["steps"][0]["run"]
    cli_options = {
        option
        for param in ci_group.commands["checkout"].params
        for option in param.opts
    }
    for option in ACTION_INPUT_TO_CLI_OPTION.values():
        assert option in cli_options, f"the CLI has no {option}"
        assert option in script, f"the action never passes {option}"


def test_the_action_never_falls_back_to_a_github_fetch() -> None:
    script = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]["run"]
    for forbidden in ("actions/checkout", "git clone", "github.com", "gh "):
        assert forbidden not in script


def test_failing_control_a_renamed_cli_option_is_caught() -> None:
    """The check above reads the CLI: a CLI without --host would fail it."""
    cli_options = {
        option
        for param in ci_group.commands["checkout"].params
        if param.name != "host"
        for option in param.opts
    }
    assert "--host" not in cli_options


def test_the_sync_on_miss_command_formats_the_clone_sync_argv(tmp_path: Path) -> None:
    """The seam to the host's mirror sync: the example overlay's argv template, resolved for one host, is run as is."""
    recorded = tmp_path / "argv.txt"
    script = tmp_path / "sync.sh"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$OMNI_GIT_MIRROR_ROOT" "$@" > {recorded}\n'
    )
    script.chmod(0o755)
    document = yaml.safe_load(
        resources.files("omnimarket.nodes.node_ci_checkout_effect")
        .joinpath("overlay.example.yaml")
        .read_text()
    )
    deployment = ModelCiCheckoutDeployment.model_validate(document)
    host = deployment.hosts[0]
    overlay = deployment.resolve(host.name, home=str(tmp_path))
    # The example names the mirror root through the placeholder and runs the script under `env`.
    assert overlay.sync_command[:2] == (
        "env",
        f"OMNI_GIT_MIRROR_ROOT={host.mirror_root}",
    )
    swapped = (*overlay.sync_command[:-1], str(script))
    assert CommandCloneSync(swapped, 10.0).sync("OmniNode-ai/omnimarket", "dev")
    assert recorded.read_text().split() == [host.mirror_root]
    # Resolution fills the serve address and mirror root into the source templates, and only for the declared host.
    source = overlay.sources[0]
    assert source.path_template == f"{host.mirror_root}/{{repo_name}}.git"
    assert (
        source.serve_url_template
        == f"git://{host.serve_address}:9418/{{repo_name}}.git"
    )
    with pytest.raises(ValueError, match="not in the overlay's hosts"):
        deployment.resolve("another-host", home=str(tmp_path))
