# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the two-backend suite once on this host and print one line per event (OMN-22911)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    FileReportSource,
    InMemoryReportSource,
)
from omnimarket.nodes.node_two_backend_suite_effect.adapters.resolver import (
    ImportResolver,
)
from omnimarket.nodes.node_two_backend_suite_effect.handlers.handler_two_backend_suite import (
    HandlerTwoBackendSuite,
    build_status_row,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendReport,
    ModelTwoBackendRequest,
)
from omnimarket.nodes.node_two_backend_suite_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from omnimarket.nodes.node_two_backend_suite_effect.protocols import (
    ProtocolEffectBackendReportSource,
)


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    env = os.environ if environ is None else environ
    try:
        overlay = load_overlay(env)
        source: ProtocolEffectBackendReportSource
        if overlay.report.route == "file":
            path = env.get(overlay.report.path_env)
            if not path:
                raise NodeOverlayError(
                    f"{overlay.report.path_env} is not set (overlay report.path_env)"
                )
            source = FileReportSource(Path(path))
        else:
            source = InMemoryReportSource(ModelEffectBackendReport())
        workdir_root = env.get(overlay.workdir_env) or tempfile.mkdtemp(
            prefix="two-backend-"
        )
        handler = HandlerTwoBackendSuite(
            source=source, resolver=ImportResolver(), workdir=Path(workdir_root)
        )
        result = handler.handle(
            ModelTwoBackendRequest(correlation_id=f"two-backend-{uuid.uuid4()}")
        )
        appended = build_appender(
            overlay.ledger_append, overlay.publish.lane, env
        ).append(
            build_status_row(
                result,
                lane=overlay.publish.lane,
                actor=overlay.publish.actor,
                at=datetime.now(UTC),
            )
            + "\n",
            result.correlation_id,
        )
    except (NodeOverlayError, LedgerRouteError, OSError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    for topic, body in events_of(result, load_topics()):
        sys.stdout.write(f"EVENT {topic} {json.dumps(body, sort_keys=True)}\n")
    sys.stdout.write(f"LEDGER status-row={appended.status}\n")
    sys.stdout.write(
        f"TWO-BACKEND node={NODE_NAME} status={result.status} run={len(result.runs)} "
        f"red={'yes' if result.red else 'no'} not_on_both={result.not_on_both_count}\n"
    )
    if not appended.ok:
        return 2
    return 1 if result.status == "failed" or result.red else 0


if __name__ == "__main__":
    raise SystemExit(main())
