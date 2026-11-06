# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of a host's overlay store reader, the unit the host installer renders.

Refreshes every overlay this host keeps, then writes the fleet record the floor reads when the overlay
names one. The unit passes ``--omni-home`` and ``--state-dir``. A store that does not answer is a reading,
not a failed tick: only a host with no usable snapshot exits non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.overlay_store import ModelOverlayStoreReadOverlay
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_read import (
    EXIT_UNREADABLE,
    NODE,
    build_handler,
    exit_for,
    fleet,
)


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _fleet_record(
    overlay: ModelOverlayStoreReadOverlay,
    env: Mapping[str, str],
    held: Path,
    own: list[dict[str, Any]],
) -> dict[str, Any]:
    """The newest reading of each host on the bus, with this host's own over it.

    A broker that is down can carry no reading, so this host's own says so and the others stay as last read."""
    try:
        answer: dict[str, Any] = asyncio.run(fleet(overlay, env, None))
    except Exception as exc:  # the store down is the case this record exists to show
        print(f"{NODE}: fleet not read: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        answer = (
            json.loads(held.read_text(encoding="utf-8"))
            if held.exists()
            else {"readings": {}}
        )
    for reading in own:
        answer["readings"][f"{reading['host']}:{reading['overlay_id']}"] = reading
    return answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    try:
        overlay = load_structured_overlay(NODE, ModelOverlayStoreReadOverlay, env)
        handler = build_handler(overlay, env)
    except NodeOverlayError as exc:
        print(f"{NODE}: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_UNREADABLE
    readings = asyncio.run(handler.refresh_all())
    summary = [reading.model_dump(mode="json") for reading in readings]
    print(json.dumps(summary, sort_keys=True))  # noqa: T201
    if overlay.fleet_record_relpath:
        record = args.registry_root / overlay.fleet_record_relpath
        _write_atomically(
            record, json.dumps(_fleet_record(overlay, env, record, summary), indent=2)
        )
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps({"ran_at": datetime.now(UTC).isoformat(), "readings": summary}),
    )
    return exit_for(readings)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
