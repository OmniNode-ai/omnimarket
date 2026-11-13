# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_hook_spool_reconcile_effect <verb>``.

``run``    replay the eligible dead letters and raise the alarm for the unalarmed losses.
``probe``  the same reads and the same alarm, moving no record; the reading says what is owed.

Exit codes: 0 the run was ok; 1 the run was not ok; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_hook_spool_reconcile_effect.handlers.handler_hook_spool_reconcile import (
    HandlerHookSpoolReconcile,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.models import (
    ModelHookSpoolReconcileCommand,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.overlay import (
    NODE_NAME,
    load_overlay,
)


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    spool_home = os.environ.get(overlay.spool.home_env, "").strip()
    if not spool_home:
        raise ValueError(
            f"{overlay.spool.home_env} is not set; the spool paths are relative to it"
        )
    emission_home = os.environ.get(overlay.emission.home_env, "").strip()
    if not emission_home:
        raise ValueError(
            f"{overlay.emission.home_env} is not set; the emission journal is relative to it"
        )
    handler = HandlerHookSpoolReconcile(
        overlay=overlay,
        home=Path(spool_home),
        sink=JsonlEmissionSink(Path(emission_home) / overlay.emission.journal_relpath),
        backend="real",
    )
    result = handler.handle(ModelHookSpoolReconcileCommand(replay=args.verb == "run"))
    sys.stdout.write(
        f"status={result.status} "
        f"before_pending={result.before.pending} before_dead_lettered={result.before.dead_lettered} "
        f"after_pending={result.after.pending} after_dead_lettered={result.after.dead_lettered} "
        f"replayed={result.replayed} held_refused={result.held_refused} "
        f"alarmed_losses={result.alarmed_losses}"
        f"{' reason=' + result.reason if result.reason else ''}\n"
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (
        NodeOverlayError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
