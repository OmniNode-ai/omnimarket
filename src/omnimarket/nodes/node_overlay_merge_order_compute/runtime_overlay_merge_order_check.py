# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_merge_order_compute.runtime_overlay_merge_order_check < changed_overlay_ids``.

The entry the overlay home's ci runs (OMN-22809): standard input holds the ids of the overlays the change adds or edits,
one per line. The overlay home is the clone named by the variable this node's overlay names; every pinned overlay of it
is judged through the models this interpreter has installed (the merged omnimarket tree), as node_overlay_drift_effect
judges it, and the node decides the change. A pinned overlay with no file reads ``overlay_missing``. Exit codes are the
node's: 0 ORDER_OK; 1 AHEAD_OF_CODE; 2 refused with a typed failure; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from omnimarket.events.overlay_check import EnumOverlayVerdict, ModelOverlayCheck
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    LocalOverlayHome,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_validator import (
    InProcessRuntimeValidator,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    OverlayHomeError,
    RuntimeValidatorError,
)
from omnimarket.nodes.node_overlay_merge_order_compute.handlers.handler_overlay_merge_order import (
    HandlerOverlayMergeOrder,
)
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    ModelOverlayMergeOrderCommand,
)
from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    NODE_NAME,
    ModelOverlayMergeOrderOverlay,
    load_overlay,
)
from omnimarket.nodes.node_overlay_merge_order_compute.runtime_overlay_merge_order import (
    report,
)

CHECK_CORRELATION = "overlay-merge-order-check"


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def judge_home(
    overlay: ModelOverlayMergeOrderOverlay, environ: Mapping[str, str]
) -> tuple[ModelOverlayCheck, ...]:
    """The merged tree's answer for every pinned overlay of the home (as the drift effect reads it)."""
    home = LocalOverlayHome(
        Path(_required(overlay.home.root_env, environ)), overlay.home
    )
    pins = home.pins()
    held = set(home.overlay_ids())
    entries = tuple(
        (pin, home.text(pin.overlay_id)) for pin in pins if pin.overlay_id in held
    )
    answered = InProcessRuntimeValidator().validate(entries).results
    absent = tuple(
        ModelOverlayCheck(
            overlay_id=pin.overlay_id, verdict=EnumOverlayVerdict.OVERLAY_MISSING
        )
        for pin in pins
        if pin.overlay_id not in held
    )
    return (*answered, *absent)


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    if argv:
        sys.stderr.write(
            f"{NODE_NAME} check takes no argument: the changed overlay ids are on stdin\n"
        )
        return 3
    env = os.environ if environ is None else environ
    try:
        overlay = load_overlay(env)
        changed = tuple(
            line.strip()
            for line in (sys.stdin if stdin is None else stdin).read().splitlines()
            if line.strip()
        )
        command = ModelOverlayMergeOrderCommand(
            correlation_id=CHECK_CORRELATION,
            merged_ref=_required(overlay.merged_ref_env, env),
            changed_overlays=changed,
            merged_checks=judge_home(overlay, env),
        )
    except (
        NodeOverlayError,
        OverlayHomeError,
        RuntimeValidatorError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    return report(
        HandlerOverlayMergeOrder(overlay).handle(command),
        overlay,
        sys.stdout,
        sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
