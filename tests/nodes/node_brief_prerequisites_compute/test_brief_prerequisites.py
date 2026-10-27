# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A caller's pin is a preference; explicit brief prerequisites bind placement."""

import pytest

from omnimarket.nodes.node_brief_prerequisites_compute import (
    NodeBriefPrerequisitesCompute,
)
from omnimarket.nodes.node_brief_prerequisites_compute.models.model_brief_prerequisites import (
    ModelBriefPrerequisitesRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("brief", "pin", "host", "required", "inputs"),
    [
        ("Work.", None, None, False, ()),
        ("Work.", "host-a", "host-a", False, ()),
        ("Required host: host-a", None, "host-a", True, ()),
        ("Host-local inputs: host-a:/tmp/input", None, "host-a", True, ("/tmp/input",)),
        ("Host-local inputs: ~/input", "host-a", "host-a", True, ("~/input",)),
        ("```text\nRequired host: host-b\n```\nWork.", "host-a", "host-a", False, ()),
        ("> Required host: host-b\nWork.", "host-a", "host-a", False, ()),
        (" Required host: host-b\nWork.", "host-a", "host-a", False, ()),
    ],
)
def test_prerequisite_metadata(brief, pin, host, required, inputs):
    result = NodeBriefPrerequisitesCompute().handle(
        ModelBriefPrerequisitesRequest(brief, pin)
    )
    assert (result.host, result.required, result.inputs) == (host, required, inputs)


@pytest.mark.parametrize(
    ("brief", "pin", "error"),
    [
        ("Required host: host-a", "host-b", "conflicting host"),
        ("Host-local inputs: /tmp/input", None, "require a host"),
        ("Required host: host-a\nHost-local inputs: relative", None, "rooted paths"),
        ("Host-local inputs:", "host-a", "at least one path"),
        ("Required host: host-a host-b", None, "one pool host"),
    ],
)
def test_invalid_prerequisites_refuse(brief, pin, error):
    with pytest.raises(ValueError, match=error):
        NodeBriefPrerequisitesCompute().handle(
            ModelBriefPrerequisitesRequest(brief, pin)
        )
