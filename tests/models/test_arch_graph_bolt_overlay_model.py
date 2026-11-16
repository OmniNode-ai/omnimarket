# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The arch_graph_bolt overlay model accepts a Bolt URI with a host and nothing else (OMN-24687)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.node_overlays.model_arch_graph_bolt_overlay import (
    ModelArchGraphBoltHostBinding,
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
    assert ModelArchGraphBoltHostBinding(host="h", bolt_uri=uri).bolt_uri == uri


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
    ],
)
def test_arch_graph_bolt_uri_without_a_host_or_with_trailing_text_is_refused(
    uri: str,
) -> None:
    with pytest.raises(ValidationError):
        ModelArchGraphBoltHostBinding(host="h", bolt_uri=uri)
