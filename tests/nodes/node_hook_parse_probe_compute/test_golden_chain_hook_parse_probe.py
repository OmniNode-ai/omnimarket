# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and the probe names the unmounted volume (OMN-22687)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_hook_parse_probe_compute.models import (
    ModelHookParseProbeRequest,
)
from tests.nodes.node_hook_parse_probe_compute.probe_world import (
    home_with_installed_plugin,
    mount,
    render,
    run_probe,
    volume_layout,
)

NAME = "node_hook_parse_probe_compute"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.hook-parse-probe-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.hook-parse-probe-rendered.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "render_hook_parse_probe"
    }


def test_golden_chain_an_unmounted_volume_is_named_and_the_host_is_still_refused(
    tmp_path: Path,
) -> None:
    """A lab host, 2026-11-06T14:11Z: no console login after a reboot, the external volume stayed unmounted, the registry link
    dangled. The recorded reading was ``HOOKPARSE unknown marketplace-unreadable:no-directory-source-for-onex@omninode-tools-dev``."""
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    home, _ = volume_layout(tmp_path, volumes, "OmniDataAbsent")
    script = (
        _handler().handle(ModelHookParseProbeRequest(volumes_root=str(volumes))).script
    )
    line = run_probe(home, script, tmp_path)
    assert line == (
        f"HOOKPARSE unknown marketplace-unreadable:volume-not-mounted-{volumes}/OmniDataAbsent"
    )


def test_golden_chain_a_mounted_volume_reads_ok_through_the_directory_marketplace(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / "volumes"
    home, market = volume_layout(tmp_path, volumes, "OmniData")
    mount(market.resolve())
    line = run_probe(home, render(volumes_root=str(volumes)), tmp_path)
    assert line.startswith("HOOKPARSE ok "), line


def test_golden_chain_a_host_with_no_directory_marketplace_reads_ok(
    tmp_path: Path,
) -> None:
    home = home_with_installed_plugin(tmp_path)
    assert run_probe(home, render(), tmp_path) == "HOOKPARSE ok 1 1"
