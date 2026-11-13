# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves, and a host that cannot resolve a peer's name reads ok through its fallback (OMN-24279)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_host_capability_compute.handlers.handler_host_capability import (
    parse_capability_lines,
)
from tests.nodes.node_lab_ssh_probe_compute.probe_world import (
    H202_ROWS,
    home,
    render,
    row,
    run_probe,
)

NAME = "node_lab_ssh_probe_compute"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    handlers = contract["handler_routing"]["handlers"]
    assert {e["operation"] for e in handlers} == {"render_lab_ssh_probe"}
    module = importlib.import_module(handlers[0]["handler"]["module"])
    assert hasattr(module, handlers[0]["handler"]["name"])


def test_golden_chain_every_peer_answers_on_its_target() -> None:
    rows = (
        row("h200", "m1"),
        row("h051", "h051", "hp-gen8"),
        row("h053", "h053", "hp-gen9"),
    )
    top, env = home(("h051", "h053"))
    line = run_probe(render("h200", rows).script, top, env)
    assert line == "PARITY-CAP lab-ssh ok reached:h051,h053"


def test_golden_chain_h202_reads_ok_through_the_fallbacks_of_peers_it_cannot_resolve() -> (
    None
):
    """h202, 2026-11-13: ``Could not resolve hostname`` for h051 and h053; the declared fallbacks hp-gen8 and hp-gen9 answer."""
    top, env = home(("hp-gen8", "hp-gen9"))
    rendered = render("h202", H202_ROWS)
    assert rendered.peers == ("h051", "h053")
    line = run_probe(rendered.script, top, env)
    assert line == "PARITY-CAP lab-ssh ok reached:hp-gen8,hp-gen9"


def test_golden_chain_the_line_parses_into_the_capability_the_probe_reader_judges() -> (
    None
):
    top, env = home(("hp-gen8", "hp-gen9"))
    line = run_probe(render("h202", H202_ROWS).script, top, env)
    (capability,) = parse_capability_lines(line)
    assert (capability.name, capability.verdict) == ("lab-ssh", "ok")
    assert capability.detail == "reached:hp-gen8,hp-gen9"
