# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 probe carries no suppression comment (OMN-19966).

The canonical-file-shape ratchet refuses a change that grows the count of suppression comments, so the loopback model
server URL is built from its parts and no line is annotated.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers import (
    probe_c30,
)


def test_probe_source_has_no_suppression_comment() -> None:
    source = Path(probe_c30.__file__).read_text(encoding="utf-8")
    assert "url-authority-ok" not in source


def test_loopback_endpoint_is_built_from_the_bound_socket() -> None:
    with probe_c30.loopback_model_server() as endpoint:
        assert endpoint.startswith("http://127.0.0.1:")
        assert endpoint.endswith("/v1/chat/completions")
