# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReceiptGatePinFrictionLedger - a friction event as a FRICTION ledger row (OMN-20134).

A pin pull request whose updater run failed, or that a lane or person touched, is
process friction. The existing friction readers (the rollup, the morning sweep, the
friction ticket aging) read FRICTION rows of the rolling ledger by their ``class=``
cell, so this effect appends one such row, ``class=pin-advance`` with ``site=`` the
caller repository, through the ledger host's bus append (the same canonical path the
pr-handoff effect uses). It answers exactly once per friction event, whatever happened,
and says nothing about a fold that caused none.

The ledger request id is a uuid5 of the event's site, branch and reason, so a
redelivered event is the same request and the ledger host appends it once.
"""

from __future__ import annotations

import logging
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

import yaml

from omnimarket.models.receipt_gate_pin_friction import (
    ModelPinFrictionEvent,
    ModelPinFrictionFoldResult,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.adapters import (
    appender_from_environment,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.models import (
    EnumPinFrictionLedgerStatus,
    ModelPinFrictionLedgerAppended,
    ModelPinFrictionLedgerConfig,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.protocols import (
    ProtocolPinFrictionLedgerAppender,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_CONFIG_KEY = "pin_friction_ledger_config"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_ROW_BREAKERS = re.compile(r"[|\r\n]+")
_STATUS: dict[EnumWorkLedgerAppendStatus, EnumPinFrictionLedgerStatus] = {
    EnumWorkLedgerAppendStatus.ACCEPTED: EnumPinFrictionLedgerStatus.ACCEPTED,
    EnumWorkLedgerAppendStatus.DUPLICATE: EnumPinFrictionLedgerStatus.DUPLICATE,
    EnumWorkLedgerAppendStatus.REFUSED: EnumPinFrictionLedgerStatus.REFUSED,
    EnumWorkLedgerAppendStatus.ERROR: EnumPinFrictionLedgerStatus.ERROR,
}


def load_ledger_config(
    contract_path: Path = _CONTRACT_PATH,
) -> ModelPinFrictionLedgerConfig:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelPinFrictionLedgerConfig.model_validate(contract[_CONFIG_KEY])


def request_id_of(
    event: ModelPinFrictionEvent, config: ModelPinFrictionLedgerConfig
) -> UUID:
    """One ledger request per (site, branch, reason): a redelivered event is the same request."""
    return uuid5(
        config.request_namespace, f"{event.site}|{event.branch}|{event.reason.value}"
    )


def _clean(text: str) -> str:
    """The ledger is pipe-delimited and line-oriented: a pipe or newline would forge a cell or a row."""
    return _ROW_BREAKERS.sub(" ", text).strip()


def build_friction_row(
    event: ModelPinFrictionEvent,
    *,
    config: ModelPinFrictionLedgerConfig,
    stamp: datetime,
    request_id: UUID,
    host: str,
) -> str:
    """The FRICTION row for one event: class and site cells first, a one-line account last."""
    refusals = ",".join(c.value for c in event.refusal_classes) or "none"
    cells = [
        f"{stamp.astimezone(UTC).strftime(_STAMP)}",
        "FRICTION",
        f"lane={config.lane}",
        f"actor={config.actor}",
        "model=none",
        f"class={config.friction_class}",
        f"site={event.site}",
        f"reason={event.reason.value}",
        f"pin_kind={event.pin_kind.value}",
        f"core_tag={event.core_tag}",
        f"branch={event.branch}",
    ]
    if event.pr_number is not None:
        cells.append(f"pr={event.site.split('/')[-1]}#{event.pr_number}")
    cells += [
        f"fix_pushes={event.fix_pushes}",
        f"ci_reruns={event.ci_reruns}",
        f"refusals={refusals}",
        f"existing={config.ticket}",
        f"cost={config.cost_by_reason[event.reason]}",
        f"req={request_id}",
        f"via=bus:{host}",
    ]
    account = (
        f"Pin pull request at {event.site} for {event.core_tag} ({event.pin_kind.value} pin): "
        f"{event.reason.value}"
        + (f"; {_clean(event.detail)}" if event.detail else "")
        + f". Measured by node_projection_receipt_gate_pin_friction ({config.ticket})."
    )
    return " | ".join([*cells, _clean(account)])


class HandlerReceiptGatePinFrictionLedger:
    """One ``ModelPinFrictionLedgerAppended`` per friction event, accepted to pending."""

    def __init__(
        self,
        appender: ProtocolPinFrictionLedgerAppender | None = None,
        host_name: str | None = None,
        *,
        config: ModelPinFrictionLedgerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config if config is not None else load_ledger_config()
        # The runtime constructs the handler bare; the deployment's lane wires the
        # appender. With none, every event is answered error, never silence.
        self._appender = (
            appender
            if appender is not None
            else appender_from_environment(
                lane_env=self._config.ledger_bus_lane_env,
                root_env=self._config.workspace_root_env,
            )
        )
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, folded: ModelPinFrictionFoldResult
    ) -> ModelPinFrictionLedgerAppended | None:
        """Append the fold's friction event; a fold that caused none is nothing."""
        if folded.friction_event is None:
            return None
        return await self.append(folded.friction_event)

    async def append(
        self, event: ModelPinFrictionEvent
    ) -> ModelPinFrictionLedgerAppended:
        config = self._config
        request_id = request_id_of(event, config)
        now = self._clock()
        row = build_friction_row(
            event, config=config, stamp=now, request_id=request_id, host=self._host
        )

        def answer(
            status: EnumPinFrictionLedgerStatus,
            message: str,
            receipt: ModelWorkLedgerAppendReceipt | None = None,
        ) -> ModelPinFrictionLedgerAppended:
            return ModelPinFrictionLedgerAppended(
                request_id=request_id,
                status=status,
                row=row,
                ledger_lines=tuple(receipt.ledger_lines) if receipt else (),
                ledger_host=receipt.ledger_host if receipt else "",
                message=message[-2000:],
                answered_at=self._clock(),
            )

        if self._appender is None:
            return answer(
                EnumPinFrictionLedgerStatus.ERROR,
                f"no ledger appender is wired: set {config.ledger_bus_lane_env} "
                f"and {config.workspace_root_env}",
            )
        request = ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=row + "\n",
            requested_by_lane=config.lane,
            requesting_host=self._host,
            requested_at=now,
        )
        try:
            receipt = await self._appender.append(request, timeout_s=config.timeout_s)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("pin friction ledger append %s failed: %s", request_id, exc)
            return answer(
                EnumPinFrictionLedgerStatus.ERROR, f"{type(exc).__name__}: {exc}"
            )
        if receipt is None:
            return answer(
                EnumPinFrictionLedgerStatus.PENDING,
                f"no receipt within {config.timeout_s:.0f}s; resend under the same request id",
            )
        if receipt.request_id != request_id:
            return answer(
                EnumPinFrictionLedgerStatus.ERROR,
                f"the ledger answered for request {receipt.request_id}",
                receipt,
            )
        return answer(_STATUS[receipt.status], receipt.message, receipt)


__all__ = [
    "HandlerReceiptGatePinFrictionLedger",
    "build_friction_row",
    "load_ledger_config",
    "request_id_of",
]
