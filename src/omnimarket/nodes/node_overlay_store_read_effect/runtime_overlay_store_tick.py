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
import faulthandler
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.floor_projection import publish_envelope
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.overlay_store import ModelOverlayStoreReadOverlay
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_read import (
    EXIT_UNREADABLE,
    NODE,
    _contract,
    build_handler,
    exit_for,
    fleet,
)


def tick_deadline_s() -> float:
    """The contract's bound on one tick; past it the process ends and the next tick starts clean."""
    return float(_contract()["descriptor"]["timeout_ms"]) / 1000


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _held_readings(held: Path) -> dict[str, dict[str, Any]]:
    """The readings the last tick wrote; none when the record is absent or cannot be parsed."""
    try:
        readings = json.loads(held.read_text(encoding="utf-8"))["readings"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return dict(readings) if isinstance(readings, dict) else {}


def _newer(
    held: dict[str, dict[str, Any]], read: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The held readings with each read one laid over it when it is not older."""
    merged = dict(held)
    for key, reading in read.items():
        kept = merged.get(key)
        if kept is None or str(kept.get("read_at", "")) <= str(
            reading.get("read_at", "")
        ):
            merged[key] = dict(reading)
    return merged


def _fleet_record(
    overlay: ModelOverlayStoreReadOverlay,
    env: Mapping[str, str],
    held: Path,
    own: list[dict[str, Any]],
) -> dict[str, Any]:
    """The newest reading of each host on the bus, with this host's own over it.

    The bus is read under this host's own consumer group, so each tick takes up where the last one stopped and
    the readings it finds are laid over the record the last tick wrote. A broker that is down can carry no
    reading, so this host's own says so and the others stay as last read."""
    readings = _held_readings(held)
    host = env.get(overlay.host_name_env, "").strip()
    try:
        answer: dict[str, Any] = asyncio.run(fleet(overlay, env, None, host))
        readings = _newer(readings, answer["readings"])
    except Exception as exc:  # the store down is the case this record exists to show
        sys.stderr.write(f"{NODE}: fleet not read: {type(exc).__name__}: {exc}\n")
    for reading in own:
        readings[f"{reading['host']}:{reading['overlay_id']}"] = reading
    return {"readings": readings}


async def _publish_fleet_projection(
    overlay: ModelOverlayStoreReadOverlay,
    env: Mapping[str, str],
    fleet_record: Mapping[str, Any],
    now: datetime,
) -> None:
    """This host's view of the fleet as a floor projection, stamped with the time this tick stood by it (OMN-23391).

    The floor reads the newest view any host published, so one host's stalled tick leaves the others' views in place and
    the projection ages only when every host's tick has stopped. The overlay accepts the id only on the broker backend."""
    if overlay.fleet_projection_id is None:
        return
    bootstrap = overlay.kafka_bootstrap
    host = env.get(overlay.host_name_env, "").strip()
    async with open_lane_bus(
        bus="kafka",
        lane=None if bootstrap else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(env),
    ) as bus:
        await publish_envelope(
            bus,
            _contract()["fleet_projection"]["topic"],
            ModelFloorProjectionEnvelope(
                projection_id=overlay.fleet_projection_id,
                as_of=now,
                producer=f"{NODE}:{host}",
                payload=dict(fleet_record),
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    # A consumer caught mid-join when its broker goes away can retry its coordinator forever, and a unit
    # manager starts no second tick while one runs: measured on a lab host, 18 minutes and counting.
    faulthandler.dump_traceback_later(tick_deadline_s(), exit=True)
    try:
        overlay = load_structured_overlay(NODE, ModelOverlayStoreReadOverlay, env)
        handler = build_handler(overlay, env)
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    readings = asyncio.run(handler.refresh_all())
    summary = [reading.model_dump(mode="json") for reading in readings]
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    if overlay.fleet_record_relpath:
        record = args.registry_root / overlay.fleet_record_relpath
        fleet_record = _fleet_record(overlay, env, record, summary)
        _write_atomically(record, json.dumps(fleet_record, indent=2))
        try:
            asyncio.run(
                _publish_fleet_projection(overlay, env, fleet_record, datetime.now(UTC))
            )
        except (
            Exception
        ) as exc:  # the broker down is the case the projection's age shows
            sys.stderr.write(
                f"{NODE}: fleet projection not published: {type(exc).__name__}: {exc}\n"
            )
    _write_atomically(
        args.state_dir / "last-run.json",
        json.dumps({"ran_at": datetime.now(UTC).isoformat(), "readings": summary}),
    )
    return exit_for(readings)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
