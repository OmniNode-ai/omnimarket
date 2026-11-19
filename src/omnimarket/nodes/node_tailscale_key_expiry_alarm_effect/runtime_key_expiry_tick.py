# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host installer's scheduled Tailscale key-expiry tick."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.runtime_adapters import (
    ChannelFromLibrary,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.adapters.tailnet_status import (
    host_aliases,
    read_tailscale_status,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.handlers.handler_key_expiry_alarm import (
    HandlerKeyExpiryTick,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.models import (
    ModelKeyExpiryTickRequest,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.overlay import (
    NODE_NAME,
    load_overlay,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--probe", action="store_true", help="Read without writing or sending"
    )
    args = parser.parse_args(argv)

    try:
        overlay = load_overlay()
        handler = HandlerKeyExpiryTick(
            overlay=overlay,
            aliases=host_aliases(resolve_lab_hosts_table(), overlay.host_aliases),
            state_dir=args.state_dir,
            read_status_document=read_tailscale_status,
            channel=ChannelFromLibrary(
                args.registry_root / overlay.channel_library, 30
            ),
        )
        result = handler.handle(
            ModelKeyExpiryTickRequest(now=utc_now(), probe=args.probe)
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE_NAME}: {exc}\n")
        return 3
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            f"{NODE_NAME}: {type(exc).__name__} reading settings or state\n"
        )
        return 3

    sys.stdout.write(result.model_dump_json() + "\n")
    if result.push_refused:
        return 2
    return 3 if result.source == "unavailable" else 0


if __name__ == "__main__":
    sys.exit(main())
