# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_tailnet_https_serve_effect install|uninstall|status``.

Exit codes: 0 the outcome is the thing asked for (installed, already installed, uninstalled, not installed, or a
status read); 1 any refusal or failure; 3 a deployment fact is missing (no overlay); 4 the run's STATUS row could
not be appended.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tailnet_https_serve_effect.container.container_tailnet_https_serve_effect import (
    create_handler_tailnet_https_serve,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    ModelServeRequest,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.overlay import load_overlay
from omnimarket.nodes.node_tailnet_https_serve_effect.protocols import ServeStepError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=[mode.value for mode in EnumServeMode])
    args = parser.parse_args(argv)
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE {exc}" + "\n")
        return 3
    started = datetime.now(UTC)
    request = ModelServeRequest(
        run_id=started.strftime("%Y%m%dT%H%M%SZ"), mode=EnumServeMode(args.verb)
    )
    try:
        result = create_handler_tailnet_https_serve(overlay).handle(request)
    except ServeStepError as exc:
        sys.stderr.write(f"REPORT-FAILED {exc}" + "\n")
        return 4
    sys.stdout.write(
        f"{result.outcome.value} run={result.run_id} port={result.https_port} {result.detail}\n"
    )
    return 0 if result.ok else 1
