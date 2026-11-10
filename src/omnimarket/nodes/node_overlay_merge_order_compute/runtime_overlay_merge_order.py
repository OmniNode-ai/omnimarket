# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_merge_order_compute < command.json``.

Reads one ``ModelOverlayMergeOrderCommand`` as JSON on standard input, decides it under the node's overlay and prints
the decision. Exit codes: 0 ORDER_OK; 1 AHEAD_OF_CODE (each ahead overlay printed with its refused fields, never a
value); 2 the check was refused with a typed failure (nothing is passed); 3 a deployment fact is missing or the input is
not a command (INDETERMINATE).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_overlay_merge_order_compute.handlers.handler_overlay_merge_order import (
    HandlerOverlayMergeOrder,
)
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    EnumMergeOrderVerdict,
    ModelOverlayMergeOrderCommand,
)
from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    NODE_NAME,
    load_overlay,
)


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        sys.stderr.write(
            f"{NODE_NAME} takes no argument: the command is JSON on stdin\n"
        )
        return 3
    try:
        overlay = load_overlay()
        command = ModelOverlayMergeOrderCommand.model_validate_json(sys.stdin.read())
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    result = HandlerOverlayMergeOrder(overlay).handle(command)
    if result.status == "failed":
        sys.stderr.write(f"OVERLAY-MERGE-ORDER-REFUSED {result.failure}\n")
        return 2
    assert result.verdict is not None
    sys.stdout.write(
        f"DECISION verdict={result.verdict.value} merged_ref={result.merged_ref} "
        f"changed={result.changed} accepted={len(result.accepted)} ahead={len(result.ahead)}\n"
    )
    for item in result.ahead[: overlay.max_named]:
        shown = "+".join(item.fields) or "-"
        sys.stdout.write(
            f"AHEAD overlay={item.overlay_id} verdict={item.verdict} fields={shown}\n"
        )
    return 1 if result.verdict is EnumMergeOrderVerdict.AHEAD_OF_CODE else 0


if __name__ == "__main__":
    raise SystemExit(main())
