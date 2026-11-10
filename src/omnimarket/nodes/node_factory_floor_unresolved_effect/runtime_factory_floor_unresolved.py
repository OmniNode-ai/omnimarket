# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_factory_floor_unresolved_effect --home <registry home>``.

Reads the unresolved ledger evidence and writes the record the factory floor reads. Exit 0 when the record was written; 65 when
the overlay cannot be read; 1 when the reads failed or the record could not be written (the previous record stays).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_unresolved_rows_record import (
    ModelUnresolvedRowsRecord,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    load_contract,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.handlers import (
    HandlerFactoryFloorUnresolved,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
    ModelFactoryFloorUnresolvedRequest,
)

NODE = "node_factory_floor_unresolved_effect"
EXIT_UNREADABLE = 65
EXIT_NOT_WRITTEN = 1


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def run(
    home: Path,
    overlay: ModelFactoryFloorUnresolvedOverlay,
    sink: ProtocolEmissionSink,
    *,
    plugin_root: str | None,
    stand_in_file: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ModelUnresolvedRowsRecord:
    """One pass: read, record the chain, write the record. A failed read leaves the previous record in place."""
    contract = _contract()["runtime"]
    now = clock or (lambda: datetime.now(UTC))
    chain = ChainEmitter(
        sink, node=contract["chain"], piece=int(contract["piece"]), clock=clock
    )
    chain.start()
    handler = HandlerFactoryFloorUnresolved(overlay, clock=clock)
    try:
        record = handler.handle(
            ModelFactoryFloorUnresolvedRequest(
                read_at=now(), plugin_root=plugin_root, stand_in_file=stand_in_file
            )
        )
    except Exception as exc:
        chain.failure("unresolved_read_failed", f"{type(exc).__name__}: {exc}")
        raise
    output = (
        home / load_contract(OWN_CONTRACT_PATH).runtime.sources.unresolved_rows_relpath
    )
    try:
        _write_atomically(output, record.model_dump_json(indent=2))
    except OSError as exc:
        chain.failure("record_not_written", type(exc).__name__)
        raise
    chain.success({"rows": len(record.rows), "seconds": int(record.seconds)})
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    contract = _contract()["runtime"]
    try:
        overlay = load_structured_overlay(
            contract["overlay"]["overlay_id"],
            ModelFactoryFloorUnresolvedOverlay,
            env,
            file_env=contract["overlay"]["file_env"],
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    plugin_root = env.get(
        load_contract(OWN_CONTRACT_PATH).runtime.plugin_root_env, ""
    ).strip()
    sink = JsonlEmissionSink(args.home / contract["journal_relpath"])
    try:
        record = run(
            args.home,
            overlay,
            sink,
            plugin_root=plugin_root or None,
            clock=lambda: datetime.now(UTC),
        )
    except Exception as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return EXIT_NOT_WRITTEN
    sys.stdout.write(
        f'{{"rows": {len(record.rows)}, "seconds": {record.seconds:.1f}}}\n'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run"]
