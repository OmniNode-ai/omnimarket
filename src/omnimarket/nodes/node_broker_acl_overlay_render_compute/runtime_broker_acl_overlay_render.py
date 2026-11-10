# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-line entry point: render an overlay and print the result as JSON."""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    BrokerAclOverlayRenderError,
    HandlerBrokerAclOverlayRender,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.models.model_broker_acl_render import (
    ModelBrokerAclRenderRequest,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.overlay import (
    load_acl_overlay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the desired broker ACL rows from a permissions overlay."
    )
    parser.add_argument("--overlay-id", required=True, help="Overlay id to load and render.")
    args = parser.parse_args(argv)
    try:
        overlay = load_acl_overlay(args.overlay_id)
    except Exception as error:
        print(f"overlay error: {error}", file=sys.stderr)
        return 2
    try:
        result = HandlerBrokerAclOverlayRender().handle(
            ModelBrokerAclRenderRequest(overlay=overlay)
        )
    except BrokerAclOverlayRenderError as error:
        print(f"render error: {error}", file=sys.stderr)
        return 2
    print(result.model_dump_json(indent=2))
    return 0
