# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_proof_judge_use_effect record``.

Reads one JSON ModelLabProofJudgeUseRecord from stdin, builds the real adapters from the
overlay and the environment, records the use, and prints the result JSON.

Exit codes: 0 both the event and the row were accepted; 1 either failed; 3 a deployment
fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_proof_judge_use_effect.handlers.handler_lab_proof_judge_use import (
    HandlerLabProofJudgeUse,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.models.model_lab_proof_judge_use_record import (
    ModelLabProofJudgeUseRecord,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    NODE_NAME,
    ModelLabProofJudgeUseOverlay,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def build_handler(
    overlay: ModelLabProofJudgeUseOverlay,
    environ: Mapping[str, str],
) -> HandlerLabProofJudgeUse:
    """Build the handler with the real adapters from the overlay and environment."""
    host = environ.get(overlay.ledger.append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger.append.host_env} is not set; the {NODE_NAME} overlay names it"
        )
    appender = build_appender(overlay.ledger.append, overlay.ledger.lane, environ)
    publisher = EmitEffectTopicPublisher()
    return HandlerLabProofJudgeUse(
        overlay=overlay,
        host=host,
        appender=appender,
        publisher=publisher,
    )


def _record() -> int:
    overlay = load_overlay()
    handler = build_handler(overlay, os.environ)
    raw = sys.stdin.read()
    record = ModelLabProofJudgeUseRecord.model_validate_json(raw)
    result = handler.handle(record)
    sys.stdout.write(result.model_dump_json() + "\n")
    ok = result.event_published and result.ledger in ("written", "already-written")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("record",))
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.verb == "record":
            return _record()
        return 1
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
