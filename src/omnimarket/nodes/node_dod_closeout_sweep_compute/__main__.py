# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dod_closeout_sweep_compute's scope bundle (OMN-20655).

    python -m omnimarket.nodes.node_dod_closeout_sweep_compute bundle \\
        --candidates C.json --pr-state P.json --text-state T.json --max 12 \\
        [--contract-state K.json] [--overlay overlay.yaml]

Without --contract-state the contract index reads contracts/ on each overlay repository's
default branch under OMNI_HOME (the real backend); the handler itself reads nothing. Prints
the bounded bundle as one JSON object, or exits 2 naming what was unreadable; an input the
bundle cannot read is never an empty bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    GitCloneContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    ScopeInputUnreadableError,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloseoutDecisionKind,
    ModelDodCloseoutDecisionRequest,
)


def read_array(path: Path, name: str) -> list[dict[str, object]]:
    try:
        # node-purity-ok: CLI boundary reads its argument files
        text = path.read_text(encoding="utf-8")  # node-purity-ok: CLI boundary
    except OSError as exc:
        raise ScopeInputUnreadableError(f"{name} {path}: unreadable: {exc}") from exc
    try:
        rows = json.loads(text)
    except ValueError as exc:
        raise ScopeInputUnreadableError(f"{name} {path}: not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ScopeInputUnreadableError(
            f"{name} {path}: not a JSON array ({type(rows).__name__})"
        )
    return [r if isinstance(r, dict) else {} for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    bundle = sub.add_parser(
        "bundle", help="the bounded scope the workflow selects from"
    )
    bundle.add_argument("--candidates", type=Path, required=True)
    bundle.add_argument("--pr-state", type=Path, required=True)
    bundle.add_argument("--text-state", type=Path, required=True)
    bundle.add_argument("--max", type=int, required=True, dest="limit")
    bundle.add_argument("--contract-state", type=Path)
    bundle.add_argument("--overlay", type=Path)
    bundle.add_argument(
        "--extra-clone-root",
        type=Path,
        action="append",
        default=[],
        help="a clone outside the registry root whose contracts/ also counts (repeatable)",
    )
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--max must be a positive integer")
    try:
        overlay = load_scope_overlay(args.overlay)
        candidates = read_array(args.candidates, "--candidates")
        if args.contract_state is not None:
            contract_state = read_array(args.contract_state, "--contract-state")
        else:
            # node-purity-ok: CLI boundary names the registry root
            home = os.environ.get("OMNI_HOME", "")  # node-purity-ok: CLI boundary
            if not home:
                raise ScopeInputUnreadableError("OMNI_HOME is not set")
            ids = GitCloneContractIndex(
                Path(home), overlay, args.extra_clone_root
            ).contract_ids()
            contract_state = [
                {"id": c["id"], "has_contract": c["id"] in ids}
                for c in candidates
                if isinstance(c.get("id"), str)
            ]
        request = ModelDodCloseoutDecisionRequest(
            kind=EnumCloseoutDecisionKind.SCOPE_BUNDLE,
            date="1970-01-01",
            scope_candidates=candidates,
            scope_pr_state=read_array(args.pr_state, "--pr-state"),
            scope_text_state=read_array(args.text_state, "--text-state"),
            scope_contract_state=contract_state,
            scope_overlay=overlay,
            max_candidates=args.limit,
        )
        result = HandlerDodCloseoutSweep().handle(request)
    except (ScopeInputUnreadableError, OSError, ValueError) as exc:
        sys.stderr.write(f"closeout-scope: REFUSED: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(result.scope_bundle, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
