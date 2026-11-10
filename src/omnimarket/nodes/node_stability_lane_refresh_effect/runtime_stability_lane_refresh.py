# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_stability_lane_refresh_effect <verb>``.

``refresh``  the plan from the committed pin (add ``--execute`` to refresh the lane). A tag set other than
             the pin's needs ``--bump`` and ``--infra/--core/--market`` tags; a dev tag is refused.
``health``   read the lane's /health and compare it with the pin: version, environment, wiring.

Prints the typed result as one JSON object. Exit codes: 0 plan printed, lane already at the pin, refreshed or
healthy; 64 a tag is not a release tag of a clone; 65 the requested set is not the pin's and carries no
``--bump``; 66 the pin file is unreadable; 3 the overlay is missing; any other nonzero is the refresh's own.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    FileAppliedPinStore,
    FilePinSource,
    GitTagCheck,
    HttpLaneHealth,
    SubprocessRefreshExecutor,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    EXIT_REFUSED_NOT_RELEASE_TAG,
    HandlerStabilityLaneRefresh,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelReleasePinSet,
    ModelStabilityRefreshCommand,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import load_overlay


def build_handler() -> HandlerStabilityLaneRefresh:
    overlay = load_overlay()
    return HandlerStabilityLaneRefresh(
        overlay=overlay,
        pin_source=FilePinSource(overlay.pin_file),
        applied=FileAppliedPinStore(overlay.state_dir),
        runner=SubprocessRefreshExecutor(),
        tags=GitTagCheck(overlay.registry_root, overlay.health_timeout_seconds),
        health=HttpLaneHealth(overlay.health_url, overlay.health_timeout_seconds),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node_stability_lane_refresh_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    refresh = verbs.add_parser("refresh")
    refresh.add_argument("--execute", action="store_true")
    refresh.add_argument("--bump", action="store_true")
    refresh.add_argument("--infra")
    refresh.add_argument("--core")
    refresh.add_argument("--market")
    verbs.add_parser("health")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        handler = build_handler()
    except NodeOverlayError as error:
        sys.stderr.write(f"overlay: {error}\n")
        return 3
    if arguments.verb == "health":
        return _health(handler)
    requested = None
    given = (arguments.infra, arguments.core, arguments.market)
    if any(given):
        try:
            requested = ModelReleasePinSet(
                omnibase_infra=arguments.infra or "",
                omnibase_core=arguments.core or "",
                omnimarket=arguments.market or "",
            )
        except ValidationError as error:
            sys.stderr.write(
                f"release-tag-only: {error.error_count()} tags are not v<major>.<minor>.<patch>\n"
            )
            return EXIT_REFUSED_NOT_RELEASE_TAG
    command = ModelStabilityRefreshCommand(
        correlation_id=str(uuid.uuid4()),
        execute=arguments.execute,
        bump=arguments.bump,
        requested=requested,
    )
    result = handler.handle(command)
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


def _health(handler: HandlerStabilityLaneRefresh) -> int:
    overlay = load_overlay()
    with open(overlay.pin_file, encoding="utf-8") as pin_file:
        pin = yaml.safe_load(pin_file)["pin"]["omnibase_infra"]
    ok, detail = handler.read_health(str(pin))
    sys.stdout.write(json.dumps({"healthy": ok, "detail": detail}) + "\n")
    return 0 if ok else 1


__all__ = ["build_handler", "main"]
