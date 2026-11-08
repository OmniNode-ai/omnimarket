# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_job_start_source_effect <verb>``.

``read``   read the jobs that started in the window and keep the read in the overlay's store.
``probe``  the same read, keeping nothing.

Exit codes: 0 the window was read and its record kept (a NOT_WORKING reading is still 0: a reading
changes nothing and refuses nothing); 1 the window was not read, or the store did not keep the
record; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_job_start_source_effect.adapters.job_start_store import (
    NullJobStartStore,
)
from omnimarket.nodes.node_job_start_source_effect.handlers.handler_job_start_source import (
    HandlerJobStartSource,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    ModelJobStartRequest,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    NODE_NAME,
    ModelLedgerStore,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _run(write: bool) -> int:
    overlay = load_overlay()
    appender = (
        build_appender(overlay.store.ledger_append, overlay.store.lane)
        if write and isinstance(overlay.store, ModelLedgerStore)
        else None
    )
    handler = HandlerJobStartSource(
        overlay=overlay,
        appender=appender,
        store=None if write else NullJobStartStore(),
    )
    now = datetime.now(UTC)
    result = handler.handle(
        ModelJobStartRequest(
            correlation_id=f"job-start-{now:%Y-%m-%dT%H:%M:%SZ}", now=now
        )
    )
    count = result.job_start_count
    store = result.store.status if result.store is not None else "none"
    failure = result.read_failure.value if result.read_failure is not None else "none"
    sys.stdout.write(
        f"JOB-START backend={result.backend} verdict={result.reading.verdict.value} "
        f"starts={'unread' if count is None else count} failure={failure} "
        f"store={store} key={result.row_key} calls={result.calls}\n"
    )
    failed = result.read_failure is not None or (
        result.store is not None and result.store.failed
    )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("read", "probe"))
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return _run(write=args.verb == "read")
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
