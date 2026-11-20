# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The arch_graph_bolt overlay model accepts a Bolt URI with a host and nothing else (OMN-24687)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.node_overlays.model_arch_graph_bolt_overlay import (
    ModelArchGraphBoltOverlay,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "bolt://graph.example.test:7687",
        "bolt://graph.example.test",
        "bolt+s://graph.example.test:7687",
        "bolt+ssc://graph.example.test:7687",
        "bolt://[::1]:7687",
    ],
)
def test_arch_graph_bolt_uri_with_a_host_is_accepted(uri: str) -> None:
    assert (
        ModelArchGraphBoltOverlay(
            schema_version="arch_graph_bolt.v1", bolt_uri=uri, hosts=("h",)
        ).bolt_uri
        == uri
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "bolt:///",
        "bolt:///path",
        "bolt://:7687",
        "bolt://x:7687 extra",
        "bolt://x:7687/ extra",
        "bolt://x:notaport",
        "http://x:7687",
        "bolt://",
        # a credential never lives in a committed overlay
        "bolt://user:pw@x:7687",
        "bolt://user@x:7687",
        "bolt://:pw@x:7687",
        "bolt://@x:7687",
        # an empty port, query or fragment delimiter is trailing text, not a bare host
        "bolt://x:",
        "bolt://x:7687?",
        "bolt://x?",
        "bolt://x#",
        "bolt://x:7687#",
    ],
)
def test_arch_graph_bolt_uri_without_a_host_or_with_trailing_text_is_refused(
    uri: str,
) -> None:
    with pytest.raises(ValidationError):
        ModelArchGraphBoltOverlay(
            schema_version="arch_graph_bolt.v1", bolt_uri=uri, hosts=("h",)
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "host",
    ["lab-host-b.example.test", "lab-host-b.", ".lab-host-b", "lab.host"],
)
def test_arch_graph_bolt_dotted_host_entry_is_refused(host: str) -> None:
    # the resolver matches the short name, so a dotted entry could never be reached
    with pytest.raises(ValidationError):
        ModelArchGraphBoltOverlay(
            schema_version="arch_graph_bolt.v1",
            bolt_uri="bolt://graph.example.test:7687",
            hosts=(host,),
        )


@pytest.mark.unit
def test_arch_graph_bolt_short_host_entries_are_accepted() -> None:
    model = ModelArchGraphBoltOverlay(
        schema_version="arch_graph_bolt.v1",
        bolt_uri="bolt://graph.example.test:7687",
        hosts=("lab-host-a", "Operator-Mac"),
    )
    assert model.hosts == ("lab-host-a", "Operator-Mac")
