# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-pass-receipt condition: the lane's delivered receipt is a passing one.

The old lab alarm's lab-pass receipt, ported with its rule unchanged: the lane's delivered sha is
resolved, its receipt is read and validated, and the receipt must describe the same commit and lane
before its verdict is trusted. An unresolved sha, an unreadable, absent or malformed receipt, or a
receipt that describes another commit or lane is INDETERMINATE, never ALARM and never OK. A failing
receipt is an ALARM; a passing receipt is OK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lab_pass_receipt import (
    EnumLabPassReceiptFault,
    EnumLabPassReceiptOutcome,
    ModelLabPassReceipt,
    ModelLabPassReceiptCommand,
    ModelLabPassReceiptResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabPassReceiptSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolAlarmDelivery,
    ProtocolDeliveredShaSource,
    ProtocolLabPassReceiptSource,
    ProtocolRunRecordStore,
    ReceiptAbsentError,
    ReceiptMalformedError,
    ReceiptSurfaceUnreadableError,
    RunRecordUnwritableError,
    ShaUnresolvedError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "lab_pass_receipt"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerLabPassReceipt:
    """Read the lane's delivered receipt; a failing receipt alarms, a passing one clears it."""

    def __init__(
        self,
        *,
        settings: ModelLabPassReceiptSettings,
        sha_source: ProtocolDeliveredShaSource,
        receipts: ProtocolLabPassReceiptSource,
        delivery: ProtocolAlarmDelivery | None = None,
        records: ProtocolRunRecordStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._sha_source = sha_source
        self._receipts = receipts
        self._delivery = delivery
        self._records = records
        self._clock = clock

    def handle(self, request: ModelLabPassReceiptCommand) -> ModelLabPassReceiptResult:
        if request.fault is EnumLabPassReceiptFault.PLANTED_FAIL:
            detail = (
                f"compose lane {self._settings.lane} receipt for (injected) is FAIL on "
                f"(injected) (injected: planted-fail)"
            )
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.ALARM,
                "(injected)",
                "(injected)",
                (f"lab-pass-receipt:(injected): {detail}",),
                ("injected: planted-fail",),
                detail,
            )
        try:
            sha, surface = self._sha_source.read()
        except ShaUnresolvedError as exc:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                "(unresolved)",
                "none",
                (),
                ("sha-unresolved",),
                f"the lane's delivered sha could not be resolved, so no receipt could be looked up; an unresolved sha is not a passing lane: {exc}",
            )
        try:
            document = self._receipts.read(sha)
        except ReceiptSurfaceUnreadableError as exc:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                sha,
                surface,
                (),
                ("receipt-surface-unreadable",),
                f"the receipt surface for {sha} could not be read: {exc}",
            )
        except ReceiptAbsentError as exc:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                sha,
                surface,
                (),
                ("receipt-absent",),
                f"no receipt is present for {sha}: {exc}",
            )
        except ReceiptMalformedError as exc:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                sha,
                surface,
                (),
                ("receipt-malformed",),
                f"the receipt for {sha} is malformed: {exc}",
            )
        try:
            receipt = ModelLabPassReceipt.model_validate(document)
        except ValidationError as exc:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                sha,
                surface,
                (),
                ("receipt-malformed",),
                f"the receipt for {sha} failed validation: {exc}",
            )
        if receipt.sha != sha or receipt.lane != self._settings.lane:
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.INDETERMINATE,
                sha,
                surface,
                (),
                ("receipt-stale",),
                f"the receipt describes commit {receipt.sha} on lane {receipt.lane}, not the lane's sha {sha} on lane {self._settings.lane}",
            )
        if receipt.result == "FAIL":
            failed = [check.name for check in receipt.checks if not check.ok]
            names = ", ".join(failed) if failed else "an unnamed check"
            detail = (
                f"lab-pass-receipt:{sha}: compose lane {receipt.lane} receipt for {receipt.sha} "
                f"is FAIL on {names} (finished {receipt.finished_at.isoformat()})"
            )
            return self._finish(
                request,
                EnumLabPassReceiptOutcome.ALARM,
                sha,
                surface,
                (detail,),
                (),
                detail,
            )
        evidence = (
            f"lane {receipt.lane} receipt for {receipt.sha} is PASS over "
            f"{len(receipt.checks)} checks (surface: {surface})"
        )
        return self._finish(
            request,
            EnumLabPassReceiptOutcome.OK,
            sha,
            surface,
            (),
            (),
            evidence,
        )

    def _finish(
        self,
        request: ModelLabPassReceiptCommand,
        outcome: EnumLabPassReceiptOutcome,
        sha: str,
        surface: str,
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelLabPassReceiptResult:
        failure = ""
        if outcome is EnumLabPassReceiptOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumLabPassReceiptOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelLabPassReceiptResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            sha=sha,
            surface=surface,
            readings=(),
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._record(request, self._deliver(request, result))

    def _record(
        self, request: ModelLabPassReceiptCommand, result: ModelLabPassReceiptResult
    ) -> ModelLabPassReceiptResult:
        """Append the finished run to the run record the stale-indeterminate verb reads; a probe and a drill never write to it.

        A record that cannot be written is a typed failure of the run, never a silent skip: the stale-indeterminate
        verb would otherwise go on reading old lines as if they were this run's.
        """
        if (
            self._records is None
            or not request.write
            or request.fault is not EnumLabPassReceiptFault.NONE
        ):
            return result
        said = (
            result.evidence
            if result.outcome.value == "OK"
            else "; ".join(result.alarms or result.reasons) or result.evidence
        )
        try:
            self._records.append(
                {
                    "finished_at": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reports": [
                        {
                            "condition": CONDITION,
                            "outcome": result.outcome.value,
                            "evidence": said,
                        }
                    ],
                }
            )
        except RunRecordUnwritableError as exc:
            return result.model_copy(update={"failure": f"record-unwritable:{exc}"})
        return result

    def _deliver(
        self, request: ModelLabPassReceiptCommand, result: ModelLabPassReceiptResult
    ) -> ModelLabPassReceiptResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumLabPassReceiptFault.NONE
        ):
            return result
        try:
            typed = []
            for line in result.alarms:
                subject, separator, detail = line.partition(": ")
                if not separator:
                    raise ValueError(
                        f"alarm line {line!r} names no subject before ': '"
                    )
                typed.append(
                    ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
                )
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=CONDITION,
                    outcome=EnumDeliveryCondition(result.outcome.value),
                    alarms=tuple(typed),
                    evidence=result.evidence,
                )
            )
        except (ValueError, OSError) as exc:
            return self._with_delivery_failure(
                result, (), f"delivery-unavailable:{type(exc).__name__}"
            )
        entries = tuple(
            f"{d.key} channel={d.channel.value} ledger={d.ledger.value}"
            for d in reply.deliveries
        )
        if reply.outcome is EnumDeliveryOutcome.FAILED:
            return self._with_delivery_failure(
                result, entries, f"delivery-failed:{reply.failure}"
            )
        return result.model_copy(update={"delivery": entries})

    @staticmethod
    def _with_delivery_failure(
        result: ModelLabPassReceiptResult, entries: tuple[str, ...], failure: str
    ) -> ModelLabPassReceiptResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelLabPassReceiptResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["lab_pass_receipt_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "lab_pass_receipt_run_completed"
        if result.outcome is EnumLabPassReceiptOutcome.OK
        else "lab_pass_receipt_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "sha": result.sha,
                "surface": result.surface,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerLabPassReceipt",
    "events_for",
    "load_topics",
]
