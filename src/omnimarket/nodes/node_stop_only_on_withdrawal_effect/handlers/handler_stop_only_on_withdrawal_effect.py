# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Intent, idempotent dispatcher acknowledgement, then cursor, all via DI.

A crash after dispatch replays the same action id. A failed append or dispatcher
call cannot advance the cursor. The durable tick folds unacknowledged intents
into pending, even if a later snapshot no longer contains their stopped lane.
"""

from importlib import resources

import yaml

from omnimarket.models.model_stop_only_on_withdrawal import (
    ModelS8Applied,
    ModelS8ApplyRequest,
    ModelS8LedgerWrite,
    ModelS8PendingRepair,
    ModelS8Policy,
    ModelS8RepairAck,
)
from omnimarket.models.work_ledger_append import EnumWorkLedgerAppendStatus
from omnimarket.protocols.protocol_s8_effects import (
    ProtocolS8LedgerWriter,
    ProtocolS8TaskDispatcher,
)


class HandlerStopOnlyOnWithdrawalEffect:
    def __init__(
        self,
        ledger: ProtocolS8LedgerWriter | None = None,
        dispatcher: ProtocolS8TaskDispatcher | None = None,
        policy: ModelS8Policy | None = None,
    ) -> None:
        self._ledger = ledger
        self._dispatcher = dispatcher
        self._policy = policy or ModelS8Policy.model_validate(
            yaml.safe_load(
                resources.files("omnimarket.nodes.node_stop_only_on_withdrawal_compute")
                .joinpath("overlay.yaml")
                .read_text()
            )
        )

    async def handle(self, request: ModelS8ApplyRequest) -> ModelS8Applied:
        ledger, dispatcher = self._ledger, self._dispatcher
        if ledger is None or dispatcher is None:
            raise RuntimeError(
                "S8 requires an injected bus ledger writer and task dispatcher"
            )
        decision, tick, policy = request.decision, request.tick, self._policy
        if (
            decision.cursor.ledger_id != tick.ledger_id
            or decision.cursor.through_sequence != tick.snapshot.through_sequence
            or decision.cursor.check_id != policy.check_id
        ):
            raise ValueError("S8 decision does not belong to this tick")
        if len(decision.repairs) != len(decision.repair_writes):
            raise ValueError("S8 repair intents do not match the decision")
        pending = {
            item.repair.action_id: item
            for item in (
                *request.pending,
                *(
                    ModelS8PendingRepair(repair=repair, write=write)
                    for repair, write in zip(
                        decision.repairs, decision.repair_writes, strict=True
                    )
                ),
            )
        }

        async def append(write: ModelS8LedgerWrite) -> None:
            receipt = await ledger.append(write, tick)
            if (
                receipt.status
                not in (
                    EnumWorkLedgerAppendStatus.ACCEPTED,
                    EnumWorkLedgerAppendStatus.DUPLICATE,
                    EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
                )
                or receipt.exit_code != 0
            ):
                raise RuntimeError(f"S8 ledger append refused: {receipt.message}")

        acknowledgements: list[ModelS8RepairAck] = []
        for item in pending.values():
            repair = item.repair
            if item.write.row_id != repair.action_id:
                raise ValueError("S8 pending intent id does not match its repair")
            await append(item.write)
            ack = await dispatcher.reopen_and_redispatch(repair)
            if ack.action_id != repair.action_id or ack.scope != repair.scope:
                raise ValueError(
                    "S8 dispatcher acknowledgement does not match its repair"
                )
            acknowledgements.append(ack)
            await append(
                ModelS8LedgerWrite(
                    row_id=f"{repair.action_id}:ack",
                    row=policy.acknowledgement_row_template.format(
                        stamp=tick.tick_at.isoformat().replace("+00:00", "Z"),
                        lane=policy.observer_lane,
                        action_id=repair.action_id,
                        check=policy.check_id,
                        task=repair.scope.task_id,
                        revision=repair.scope.revision,
                        repair_key=repair.action_id.split(":", 1)[1],
                        status=ack.status,
                    ),
                )
            )
        await append(decision.cursor_write)
        return ModelS8Applied(
            acknowledgements=tuple(acknowledgements), cursor=decision.cursor
        )
