# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay environment probe: one row per declared name per host, and an unset name takes the host out of placement.

Positive control (AC1, AC2): a fixture host lacks ONEX_DEV_READINESS_HOST. It must not be chosen for the node that
declares the name, and must still be chosen for a node that does not.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_host_installer_overlay_env_probe.handlers.handler_overlay_env_probe import (
    HandlerOverlayEnvProbe,
    set_names_in,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelOverlayEnvProbeRequest,
    ModelProbeHost,
    ModelProbePlacement,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.node import (
    NodeHostInstallerOverlayEnvProbe,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_placement import (
    ModelHostTableVerdict,
)

READINESS = "ONEX_DEV_READINESS_HOST"
SIGNING = "FIXTURE_SIGNING_KEY"
SECRET_SENTINEL = "s3cr3t-value-that-must-never-appear"

R8_NODE = "node_fixture_readiness_effect"
DEPLOY_NODE = "node_fixture_deploy_effect"
PLAIN_NODE = "node_fixture_plain_effect"


def _write(root: Path, node: str, document: object) -> None:
    (root / node).mkdir(parents=True, exist_ok=True)
    (root / node / "overlay.yaml").write_text(yaml.safe_dump(document))


def _roots(tmp_path: Path) -> tuple[str, ...]:
    root = tmp_path / "overlays"
    _write(root, R8_NODE, {"dev_readiness": {"host_env": READINESS}, "port": 8085})
    _write(
        root,
        DEPLOY_NODE,
        {
            "hmac_secret_env": SIGNING,
            "secret_store": {"client_id_ref": "FIXTURE_CLIENT_ID"},
            "api_secret_ref": "llm.openrouter.api_key",
        },
    )
    _write(root, PLAIN_NODE, {"host_env": "ONEX_HOST_NAME"})
    return (str(root),)


def _verdicts(*hosts: str) -> tuple[ModelHostTableVerdict, ...]:
    return tuple(
        ModelHostTableVerdict(host_name=h, eligible=True, reason="eligible")
        for h in hosts
    )


def _hosts() -> tuple[ModelProbeHost, ModelProbeHost]:
    nodes = (R8_NODE, DEPLOY_NODE, PLAIN_NODE)
    return (
        ModelProbeHost(
            host_name="host-ok",
            nodes=nodes,
            set_names=(READINESS, SIGNING, "FIXTURE_CLIENT_ID", "ONEX_HOST_NAME"),
        ),
        ModelProbeHost(
            host_name="host-lacking",
            nodes=nodes,
            set_names=(SIGNING, "FIXTURE_CLIENT_ID", "ONEX_HOST_NAME"),
        ),
    )


def _run(tmp_path: Path, node: str) -> object:
    return NodeHostInstallerOverlayEnvProbe().handle(
        ModelOverlayEnvProbeRequest(
            overlay_roots=_roots(tmp_path),
            hosts=_hosts(),
            placement=ModelProbePlacement(
                node=node, verdicts=_verdicts("host-ok", "host-lacking")
            ),
        )
    )


def test_one_row_per_host_node_and_name_with_state(tmp_path: Path) -> None:
    result = _run(tmp_path, R8_NODE)
    states = {(r.host_name, r.node, r.name): r.state for r in result.rows}
    assert states[("host-lacking", R8_NODE, READINESS)] == "unset"
    assert states[("host-ok", R8_NODE, READINESS)] == "set"
    assert states[("host-lacking", DEPLOY_NODE, SIGNING)] == "set"
    assert len(result.rows) == len(states)
    # a store path is not a host environment name: no row for it
    assert not any("." in r.name for r in result.rows)
    assert ("host-ok", DEPLOY_NODE, "FIXTURE_CLIENT_ID") in states


def test_host_lacking_the_name_is_not_chosen_for_the_node_that_needs_it(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, R8_NODE)
    assert "host-lacking" not in result.eligible_hosts
    assert result.eligible_hosts == ("host-ok",)
    refused = {v.host_name: v for v in result.verdicts}["host-lacking"]
    assert refused.eligible is False
    assert READINESS in refused.reason


def test_same_host_is_still_chosen_for_a_node_that_does_not_need_the_name(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, PLAIN_NODE)
    assert set(result.eligible_hosts) == {"host-ok", "host-lacking"}
    deploy = _run(tmp_path, DEPLOY_NODE)
    assert "host-lacking" in deploy.eligible_hosts


def test_one_alarm_names_host_node_and_name(tmp_path: Path) -> None:
    result = _run(tmp_path, R8_NODE)
    assert len(result.alarms) == 1
    alarm = result.alarms[0]
    assert (alarm.host_name, alarm.node, alarm.name) == (
        "host-lacking",
        R8_NODE,
        READINESS,
    )
    assert "host-lacking" in alarm.message
    assert READINESS in alarm.message
    assert R8_NODE in alarm.message
    assert [(u.host_name, u.node) for u in result.unavailable] == [
        ("host-lacking", R8_NODE)
    ]


def test_no_value_is_read_carried_or_printed() -> None:
    names = set_names_in({"A_NAME": SECRET_SENTINEL, "BLANK": "  "})
    assert names == ("A_NAME",)
    host = ModelProbeHost(host_name="h", nodes=(), set_names=names)
    assert SECRET_SENTINEL not in host.model_dump_json()


def test_the_real_r8_node_declares_the_readiness_name_in_its_contract() -> None:
    declared = HandlerOverlayEnvProbe().declarations(
        "node_delegation_scenario_r8_effect", ()
    )
    assert (
        "contract",
        "runtime.dev_readiness.host_env",
        READINESS,
    ) in declared


def test_unreadable_overlay_refuses_instead_of_reading_as_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlays"
    (root / R8_NODE).mkdir(parents=True)
    (root / R8_NODE / "overlay.yaml").write_text("a: [unclosed")
    result = NodeHostInstallerOverlayEnvProbe().handle(
        ModelOverlayEnvProbeRequest(overlay_roots=(str(root),), hosts=_hosts())
    )
    assert result.status == "refused"
    assert result.exit_code == 65
    assert result.rows == ()
