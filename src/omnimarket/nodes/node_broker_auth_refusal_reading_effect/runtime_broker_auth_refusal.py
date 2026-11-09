# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_auth_refusal_reading_effect <verb>``.

``read``   read the broker's authentication refusals and append the row.
``probe``  the same read, appending nothing.

Exit codes: 0 the read ran and its row was written (NOT_WORKING is still 0: a reading changes nothing and
refuses nothing); 1 the read or its row failed (an unreadable log, an append that did not land);
3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.adapters.broker_log import (
    SshBrokerLog,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.handlers.handler_broker_auth_refusal import (
    HandlerBrokerAuthRefusal,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.models.model_broker_auth_refusal import (
    ModelBrokerAuthRefusalCommand,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    NODE_NAME,
    ModelBrokerAuthRefusalOverlay,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelBrokerAuthRefusalOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerBrokerAuthRefusal, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    _required(overlay.log_source.ssh_host_env, environ)
    handler = HandlerBrokerAuthRefusal(
        overlay=overlay,
        host=host,
        source=SshBrokerLog(overlay.log_source, environ),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelBrokerAuthRefusalCommand(
            correlation_id=f"broker-auth-refusal-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"BROKER-AUTH-REFUSAL-FAILED {result.failure}\n")
        return 1
    window = result.window
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} "
        f"refusals={window.total if window else 'unknown'} "
        f"first_seen={(window.first_seen or 'none') if window else 'unknown'} "
        f"last_seen={(window.last_seen or 'none') if window else 'unknown'} "
        f"ledger={result.ledger} key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    return 1 if result.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("read", "probe"))
        args = parser.parse_args(arguments)
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
