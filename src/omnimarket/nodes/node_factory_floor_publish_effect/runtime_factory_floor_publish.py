# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick entry: ``--omni-home`` and ``--state-dir`` are the registry module tick's two flags."""

from __future__ import annotations

import argparse
import sys

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="factory_floor_publish",
        description="Regenerate the factory floor record and publish it with its page to the serving host.",
    )
    ap.add_argument(
        "--omni-home",
        dest="registry_home",
        help="the registry home; default: the contract's home_env",
    )
    ap.add_argument("--state-dir", help="where last-run.json is written; default: none")
    ap.add_argument(
        "--skip-refresh",
        action="store_true",
        help="publish the record already on disk without running the factory floor effect",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    request = ModelFactoryFloorPublishRequest(
        home=a.registry_home, state_dir=a.state_dir, skip_refresh=a.skip_refresh
    )
    try:
        result = HandlerFactoryFloorPublish().handle(request)
    except (PublishRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED factory-floor-publish: {exc}\n")
        return 2
    sys.stdout.write(
        f"factory-floor-publish: published={','.join(result.published)} "
        f"record={result.generated_at} sha256={result.data_sha256[:12]} refreshed={result.refreshed}\n"
    )
    return 0
