# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_validator_estate_reading_effect <verb>``.

``read``   read the estate and append the row.
``probe``  the same read, appending nothing.

``--fault core-unreadable`` injects the declared fault; the row then carries ``drill=yes`` and no numbers.

Exit codes: 0 the read ran (a low share is a reading, not a failure); 1 the run failed (an append that did
not land); 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_validator_estate_reading_effect.adapters.git_estate_source import (
    GitEstateSource,
)
from omnimarket.nodes.node_validator_estate_reading_effect.handlers.handler_validator_estate import (
    HandlerValidatorEstate,
)
from omnimarket.nodes.node_validator_estate_reading_effect.models.model_validator_estate import (
    ModelValidatorEstateCommand,
)
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import load_overlay


def build_handler(write: bool) -> HandlerValidatorEstate:
    overlay = load_overlay()
    root = os.environ.get(overlay.repos_root_env, "").strip()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not root:
        raise ValueError(
            f"{overlay.repos_root_env} is not set; the overlay names it as the clones' root"
        )
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    plan_root = os.environ.get(overlay.plan.root_env, "").strip()
    if not plan_root:
        raise ValueError(
            f"{overlay.plan.root_env} is not set; the overlay names it as the plan clone's root"
        )
    return HandlerValidatorEstate(
        overlay=overlay,
        host=host,
        source=GitEstateSource(Path(root), overlay.git_timeout_seconds),
        plan_source=GitEstateSource(Path(plan_root), overlay.git_timeout_seconds),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_validator_estate_reading_effect")
    parser.add_argument("verb", choices=("read", "probe"))
    parser.add_argument("--fault", choices=("none", "core-unreadable"), default="none")
    arguments = parser.parse_args(argv)
    write = arguments.verb == "read"
    try:
        handler = build_handler(write)
    except (ValueError, LedgerRouteError) as exc:
        sys.stderr.write(f"INDETERMINATE {exc}\n")
        return 3
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelValidatorEstateCommand(
            correlation_id=f"validator-estate-{stamp}",
            write=write,
            fault=arguments.fault,
        )
    )
    sys.stdout.write(
        f"RUN outcome={result.outcome} status={result.status} ledger={result.ledger}"
        f" canonical={result.canonical}/{result.estate} other_hooks={result.other_hooks}"
        f" other_classes={result.other_classes} traces={result.canonical_emitting}/{result.canonical}"
        f" tasks_merged={result.tasks_merged}/{len(result.tasks)}\n"
    )
    for c in result.classes:
        sys.stdout.write(
            f"CLASS {c.repo} {c.module} canonical={c.canonical} data={c.rules_as_data}"
            f" literals={c.literal_tables} traces={c.emits_trace}\n"
        )
    for h in result.hooks:
        sys.stdout.write(
            f"HOOKS {h.repo} total={h.hooks} covered={h.covered} pytest={h.pytest}\n"
        )
    for t in result.tasks:
        sys.stdout.write(f"TASK {t.task} {t.state} tickets={','.join(t.tickets)}\n")
    for item in result.unreadable:
        sys.stdout.write(f"UNREADABLE {item}\n")
    if result.row:
        sys.stdout.write(f"ROW {result.row}\n")
    return 1 if result.status == "failed" else 0
