# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Selection of a lab host's ``lab-ssh`` probe peers from the lab host table's rows."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.nodes.node_host_capability_compute.handlers.handler_lab_ssh_peers import (
    HandlerLabSshPeers,
)
from omnimarket.nodes.node_host_capability_compute.models import (
    ModelLabSshPeerExclusion,
    ModelLabSshPeerRow,
    ModelLabSshPeersRequest,
    ModelLabSshPeersResult,
)

pytestmark = pytest.mark.unit

# Host names in these tests are fixture labels read from a stand-in table; the handler never sees a literal.


def table_rows() -> tuple[ModelLabSshPeerRow, ...]:
    """The lab host table as the probe reads it: five lab-owned hosts, two rows the lab does not own, one down."""
    return (
        ModelLabSshPeerRow(name="h202", target="h202"),
        ModelLabSshPeerRow(
            name="h051", target="h051", fallback_targets=("hp-gen8",), ssh_user="jonah"
        ),
        ModelLabSshPeerRow(name="h053", target="h053"),
        ModelLabSshPeerRow(name="h105", target="omnibook", lab_ssh_peer=False),
        ModelLabSshPeerRow(name="h106", target="omnidad.local", lab_ssh_peer=False),
        ModelLabSshPeerRow(name="h203", target="h203", available=False),
    )


def test_golden_peers_and_exclusions_from_h202() -> None:
    """From h202 the probe tries two peers, one token each; the table's other three rows are excluded and named."""
    result = HandlerLabSshPeers().handle(
        ModelLabSshPeersRequest(host="h202", rows=table_rows())
    )
    assert result == ModelLabSshPeersResult(
        peers=("jonah@h051+jonah@hp-gen8", "h053"),
        excluded=(
            ModelLabSshPeerExclusion(host="h202", reason="the probing host itself"),
            ModelLabSshPeerExclusion(
                host="h105", reason="not a lab-owned peer (lab_ssh_peer is false)"
            ),
            ModelLabSshPeerExclusion(
                host="h106", reason="not a lab-owned peer (lab_ssh_peer is false)"
            ),
            ModelLabSshPeerExclusion(host="h203", reason="unavailable"),
        ),
    )


def test_an_unknown_probing_host_is_refused_not_raised() -> None:
    result = HandlerLabSshPeers().handle(
        ModelLabSshPeersRequest(host="h999", rows=table_rows())
    )
    assert result == ModelLabSshPeersResult(
        peers=(),
        excluded=(),
        refusal="unknown probing host: no row of the host table is named h999",
    )


def test_control_a_host_that_is_not_ours_is_demanded_when_the_column_is_ignored() -> (
    None
):
    """Failing control: force every ``lab_ssh_peer`` true and the not-ours machines turn up as peers."""
    ignored = tuple(
        row.model_copy(update={"lab_ssh_peer": True}) for row in table_rows()
    )
    result = HandlerLabSshPeers().handle(
        ModelLabSshPeersRequest(host="h202", rows=ignored)
    )
    assert "omnibook" in result.peers
    assert "omnidad.local" in result.peers


def test_the_host_table_row_carries_lab_ssh_peer() -> None:
    """``ModelLabHostRow`` accepts ``lab_ssh_peer: false`` and defaults it to true (a machine the lab owns)."""
    assert ModelLabHostRow(name="h053", target="h053").lab_ssh_peer is True
    assert (
        ModelLabHostRow(name="h105", target="omnibook", lab_ssh_peer=False).lab_ssh_peer
        is False
    )


def test_every_routed_operation_resolves_to_a_handler_with_handle() -> None:
    package = importlib.import_module("omnimarket.nodes.node_host_capability_compute")
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    operations = {
        entry["operation"]: entry["handler"]
        for entry in contract["handler_routing"]["handlers"]
    }
    assert "select_lab_ssh_peers" in operations
    for operation, binding in operations.items():
        handler = getattr(importlib.import_module(binding["module"]), binding["name"])
        assert callable(handler.handle), operation


def test_seam_peer_row_fields_are_columns_of_the_host_table_row() -> None:
    """Seam with the ``lab_hosts`` overlay: a column the peer row reads that the table row lacks fails here."""
    assert set(ModelLabSshPeerRow.model_fields) <= set(ModelLabHostRow.model_fields)


def test_seam_every_peer_token_is_one_word_the_probe_accepts() -> None:
    """Seam with the probe shell: ``PEER_RE`` drops a token that is not one plain word, which would hide a peer."""
    from omnimarket.nodes.node_host_capability_compute.handlers.handler_host_capability import (
        PEER_RE,
    )

    result = HandlerLabSshPeers().handle(
        ModelLabSshPeersRequest(host="h202", rows=table_rows())
    )
    assert result.peers
    assert all(PEER_RE.match(token) for token in result.peers)
