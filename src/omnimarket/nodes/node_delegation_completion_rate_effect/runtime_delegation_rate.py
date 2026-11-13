# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_completion_rate_effect <verb>``.

``read``   read the window, run the completion-rate compute node and append the row (what a schedule runs).
``probe``  the same read, appending nothing.
``burn``   how fast the hosted-model credit goes (OMN-23756): the provider's balance, the spend since the paid rungs were
           switched on, dollars per hour, the projected hours until the balance and the cap are reached, and per-model calls
           and tokens from delegation_events (or ``unread`` with the reason). Reads only; exit 1 when the provider's credits
           cannot be read. ``--json`` prints the reading as JSON.

Exit codes: 0 the read ran and its row was written (NOT WORKING and UNKNOWN are still 0: a reading changes nothing and refuses
nothing); 1 the run failed (a row that did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.routing.paid_rungs import read_provider_credits
from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)
from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationRateOverlay,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    SshPostgresEventSource,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.paid_rung_calls_source import (
    SshPostgresPaidRungCallSource,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.handlers.handler_delegation_completion_rate_run import (
    HandlerDelegationCompletionRateRun,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.handlers.handler_paid_rung_burn_read import (
    HandlerPaidRungBurnRead,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_delegation_rate_run import (
    ModelDelegationRateCommand,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    NODE_NAME,
    ModelDelegationRateEffectOverlay,
    load_overlay,
    load_rate_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def build_handler(
    overlay: ModelDelegationRateEffectOverlay,
    rate_overlay: ModelDelegationRateOverlay,
    environ: Mapping[str, str],
    write: bool,
) -> HandlerDelegationCompletionRateRun:
    return HandlerDelegationCompletionRateRun(
        rate_overlay=rate_overlay,
        source=SshPostgresEventSource(overlay.events_source, environ),
        appender=build_appender(overlay.ledger_append, rate_overlay.row_lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )


def _run(write: bool) -> int:
    overlay = load_overlay()
    rate_overlay = load_rate_overlay()
    environ = os.environ  # ONEX_FLAG_EXEMPT: variable names are declared by the overlay
    handler = build_handler(overlay, rate_overlay, environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelDelegationRateCommand(correlation_id=f"delegation-rate-{now}", write=write)
    )
    if result.status == "failed":
        sys.stderr.write(f"DELEGATION-RATE-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING verdict={result.verdict.value} r_all={result.r_all} r_dep={result.r_dep} "
        f"failed_rate={result.failed_rate} rows={result.rows_in_window} ledger={result.ledger} "
        f"reason={','.join(result.reasons) or 'none'}\n"
    )
    return 0


def _burn(as_json: bool) -> int:
    overlay = load_overlay()
    paid = load_delegation_routing_overlay().paid_rungs
    if paid is None:
        sys.stderr.write(
            "INDETERMINATE: the routing overlay has no paid_rungs section\n"
        )
        return 3
    environ = os.environ  # ONEX_FLAG_EXEMPT: variable names are declared by the overlay
    result = HandlerPaidRungBurnRead(
        paid=paid,
        credits_reader=read_provider_credits,
        calls=SshPostgresPaidRungCallSource(overlay.events_source, environ),
        clock=lambda: datetime.now(UTC),
    ).handle()
    if result.reading is None:
        sys.stderr.write(f"PAID-RUNG-BURN-FAILED {result.failure}\n")
        return 1
    r = result.reading
    if as_json:
        sys.stdout.write(
            json.dumps(
                {
                    **json.loads(r.model_dump_json()),
                    "per_model": "unread"
                    if result.per_model_unread_reason
                    else json.loads(r.model_dump_json())["per_model"],
                    "per_model_unread_reason": result.per_model_unread_reason,
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    def _n(value: object) -> str:
        return (
            "n/a"
            if value is None
            else f"{value:.2f}"
            if hasattr(value, "quantize")
            else str(value)
        )

    models = (
        f"unread({result.per_model_unread_reason})"
        if result.per_model_unread_reason
        else ";".join(
            f"{m.model_id}:calls={m.calls},in={m.tokens_in},out={m.tokens_out},booked={m.booked_cost_usd}"
            for m in r.per_model
        )
        or "none"
    )
    sys.stdout.write(
        f"BURN read_at={r.read_at.strftime('%Y-%m-%dT%H:%M:%SZ')} enabled={str(r.enabled).lower()} "
        f"balance_usd={_n(r.balance_usd)} spent_since_switch_on_usd={_n(r.spent_since_switch_on_usd)} "
        f"usd_per_hour={_n(r.usd_per_hour)} hours_until_balance_gone={_n(r.hours_until_balance_gone)} "
        f"hours_until_cap_reached={_n(r.hours_until_cap_reached)} cap_usd={_n(r.spend_cap_usd)} per_model={models}\n"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("read", "probe", "burn"))
        parser.add_argument("--json", action="store_true", help="burn: print JSON")
        args = parser.parse_args(arguments)
        if args.verb == "burn":
            return _burn(as_json=args.json)
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
