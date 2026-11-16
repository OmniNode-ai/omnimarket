# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerLiveProbe."""

from __future__ import annotations

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_live_probe import (
    HandlerLiveProbe,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_live_probe import (
    ModelLiveProbeRequest,
)


def _request(probe: str) -> ModelLiveProbeRequest:
    return ModelLiveProbeRequest(probe=probe)


def test_all_names_mentioned() -> None:
    """(a) Every name mentioned with count > 0 -> base True, no notes."""
    req = _request(
        "    host_a mentions [.]node_a[.]: 3\n    host_b mentions node_b: 1\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is True
    assert result.contract_local_nodes == ()
    assert result.notes == ()


def test_zero_count_fails() -> None:
    """(b) THE FAILING CONTROL: one name with count 0 -> base False, named in a note."""
    req = _request(
        "    host_a mentions [.]node_a[.]: 0\n    host_a mentions node_b: 2\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is False
    assert result.contract_local_nodes == ()
    assert result.notes == ("changed path not live, no runtime log mentions: node_a",)


def test_term_shapes_reduce_to_node_name() -> None:
    """(c) "[.]node_x[.]" and "(node_x|contract_x)" both reduce to node_x."""
    req = _request(
        "    host_a mentions [.]node_x[.]: 0\n"
        "    host_b mentions (node_x|contract_x): 0\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is False
    assert result.contract_local_nodes == ()
    assert result.notes == ("changed path not live, no runtime log mentions: node_x",)


def test_counts_add_across_containers() -> None:
    """(d) Two containers print a line for the same name, 0 and 3 -> counts add, base True."""
    req = _request(
        "    container_a mentions node_x: 0\n    container_b mentions node_x: 3\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is True
    assert result.contract_local_nodes == ()
    assert result.notes == ()


def test_only_not_applicable_line() -> None:
    """(e) Only a not-applicable line -> base True, nodes in contract_local_nodes, one note."""
    req = _request(
        "live-grep not-applicable: node_a node_b (no topics, no runtime profile)\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is True
    assert result.contract_local_nodes == ("node_a", "node_b")
    assert result.notes == (
        "changed path not applicable, runs in no runtime (no topics, "
        "no runtime profile): node_a, node_b",
    )


def test_mentions_and_not_applicable_together() -> None:
    """(f) Mention lines plus a not-applicable line -> base follows mentions, both notes appear."""
    req = _request(
        "    host_a mentions node_a: 0\n"
        "live-grep not-applicable: node_c (no topics, no runtime profile)\n"
    )
    result = HandlerLiveProbe().handle(req)
    assert result.base is False
    assert result.contract_local_nodes == ("node_c",)
    assert result.notes == (
        "changed path not live, no runtime log mentions: node_a",
        "changed path not applicable, runs in no runtime (no topics, "
        "no runtime profile): node_c",
    )


def test_empty_probe_has_no_check() -> None:
    """(g) Empty probe text -> base None, no notes."""
    req = _request("")
    result = HandlerLiveProbe().handle(req)
    assert result.base is None
    assert result.contract_local_nodes == ()
    assert result.notes == ()
