# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_effect_bypass_scan_effect <verb>`` (follows node_join_refusal_effect/runtime_join_refusal.py).

``inventory``  scan a checkout; print the inventory as JSON, or with ``--counts-only`` the baseline row (YAML) for the repository.
``check``      scan, then judge the counts against ``--baseline`` (and ``--base-baseline``, the merge base's copy) with
              node_effect_bypass_ratchet_compute.

Exit codes: 0 no category rose; 1 a category rose, the baseline grew or the repository has no baseline row; 3 a deployment fact
is missing or unreadable (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_effect_bypass_ratchet_compute.handlers.handler_bypass_ratchet import (
    HandlerBypassRatchet,
)
from omnimarket.nodes.node_effect_bypass_ratchet_compute.models import (
    EnumBypassCategory,
    ModelBypassBaseline,
    ModelBypassRatchetRequest,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.handlers.handler_bypass_scan import (
    GitTrackedFiles,
    HandlerBypassScan,
    ProtocolTrackedFiles,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.models import (
    ModelBypassScanCommand,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.overlay import (
    NODE_NAME,
    load_policy,
)


def _read_baseline(path: str) -> ModelBypassBaseline:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ModelBypassBaseline.model_validate(document)


def _run(args: argparse.Namespace, lister: ProtocolTrackedFiles) -> int:
    root = str(Path(args.root).resolve())
    inventory = HandlerBypassScan(load_policy(), lister).handle(
        ModelBypassScanCommand(repository=args.repository, root=root)
    )
    if args.verb == "inventory":
        if args.counts_only:
            row = {c.value: inventory.counts.get(c) for c in EnumBypassCategory}
            sys.stdout.write(
                yaml.safe_dump({args.repository: {"counts": row}}, sort_keys=False)
            )
        else:
            sys.stdout.write(inventory.model_dump_json(indent=1) + "\n")
        return 0
    verdict = HandlerBypassRatchet().handle(
        ModelBypassRatchetRequest(
            inventory=inventory,
            baseline=_read_baseline(args.baseline),
            base_baseline=(
                _read_baseline(args.base_baseline) if args.base_baseline else None
            ),
        )
    )
    for reading in verdict.categories:
        sys.stdout.write(
            f"{args.repository} {reading.category.value} now={reading.now} "
            f"baseline={reading.baseline}{' ROSE' if reading.rose else ''}\n"
        )
    sys.stdout.write(
        f"{args.repository} total now={verdict.total_now} baseline={verdict.total_baseline} "
        f"ok={verdict.ok} findings={','.join(f.value for f in verdict.findings) or '-'}\n"
    )
    return 0 if verdict.ok else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    file_lister: ProtocolTrackedFiles | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("inventory", "check"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--counts-only", action="store_true")
    parser.add_argument("--baseline")
    parser.add_argument("--base-baseline")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.verb == "check" and not args.baseline:
        parser.error("check needs --baseline")
    try:
        return _run(args, file_lister or GitTrackedFiles())
    except (
        NodeOverlayError,
        ValidationError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
