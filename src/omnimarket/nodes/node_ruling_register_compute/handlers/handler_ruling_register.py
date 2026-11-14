# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read: ``handle(ModelRulingRegisterRequest) -> ModelRulingRegisterResult`` (OMN-24478).

* One verdict per policy row, in row order, on the newest reading the row took;
  a row that took no reading is UNREAD.
* A reading that did not answer, is older than the policy's bound or is dated
  after the tick is UNREAD, and so is an answer a judge cannot parse. A bad
  reading never raises; it reads UNREAD.
* A TRUE row reads no tick line; a FALSE or UNREAD row reads the MISSING line
  naming its ruling stamp, why it is not running and its owner's fix.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    EnumRulingExpect,
    EnumRulingVerdict,
    ModelRulingReading,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
    ModelRulingRow,
    ModelRulingVerdict,
)


class HandlerRulingRegister:
    """Judge the ruling register's readings against the policy; touch nothing."""

    def handle(self, request: ModelRulingRegisterRequest) -> ModelRulingRegisterResult:
        verdicts = tuple(self._judge_row(row, request) for row in request.policy.rows)
        counts = Counter(verdict.verdict for verdict in verdicts)
        return ModelRulingRegisterResult(
            correlation_id=request.correlation_id,
            passed=counts[EnumRulingVerdict.TRUE] == len(verdicts),
            verdicts=verdicts,
            true_count=counts[EnumRulingVerdict.TRUE],
            false_count=counts[EnumRulingVerdict.FALSE],
            unread_count=counts[EnumRulingVerdict.UNREAD],
        )

    def _judge_row(
        self, row: ModelRulingRow, request: ModelRulingRegisterRequest
    ) -> ModelRulingVerdict:
        reading = self._newest_reading(request.readings, row.row_id)
        if reading is None:
            verdict, detail = EnumRulingVerdict.UNREAD, "no reading taken"
        elif reading.status is EnumReadStatus.UNREAD:
            verdict, detail = (
                EnumRulingVerdict.UNREAD,
                f"read did not answer: {reading.detail}",
            )
        else:
            age_seconds = (request.now - reading.read_at).total_seconds()
            if age_seconds > request.policy.max_reading_age_seconds:
                verdict, detail = (
                    EnumRulingVerdict.UNREAD,
                    f"reading is {int(age_seconds)} seconds old, "
                    f"bound is {request.policy.max_reading_age_seconds}",
                )
            elif reading.read_at > request.now:
                verdict, detail = (
                    EnumRulingVerdict.UNREAD,
                    "reading is dated after the tick",
                )
            else:
                verdict, detail = self._judge_reading(row, reading)

        if verdict is EnumRulingVerdict.TRUE:
            why = row.must_be_true
        else:
            why = (
                f"ruling {row.ruling} not running: {row.must_be_true} "
                f"({verdict.value}: {detail}); owner {row.owner}"
            )
        return ModelRulingVerdict(
            row_id=row.row_id,
            ruling=row.ruling,
            owner=row.owner,
            verdict=verdict,
            detail=detail,
            why=why,
            fix=row.fix,
        )

    def _judge_reading(
        self, row: ModelRulingRow, reading: ModelRulingReading
    ) -> tuple[EnumRulingVerdict, str]:
        expect = row.read.expect
        if expect.kind is EnumRulingExpect.EXIT_ZERO:
            if reading.exit_code == 0:
                return EnumRulingVerdict.TRUE, "holds"
            return EnumRulingVerdict.FALSE, f"exit {reading.exit_code}"
        if expect.kind is EnumRulingExpect.STDOUT_EMPTY:
            answer = reading.stdout.strip()
            if answer:
                return EnumRulingVerdict.FALSE, answer[:120]
            return EnumRulingVerdict.TRUE, "holds"
        if expect.kind is EnumRulingExpect.STDOUT_EQUALS:
            if reading.stdout.strip() == expect.value:
                return EnumRulingVerdict.TRUE, "holds"
            return EnumRulingVerdict.FALSE, f"read {reading.stdout.strip()[:60]}"
        if expect.bound is None:
            return EnumRulingVerdict.UNREAD, "int_at_most names no bound"
        try:
            value = int(reading.stdout.strip())
        except ValueError:
            return (
                EnumRulingVerdict.UNREAD,
                f"answer is not an integer: {reading.stdout.strip()[:60]}",
            )
        if value <= expect.bound:
            return EnumRulingVerdict.TRUE, "holds"
        return EnumRulingVerdict.FALSE, f"read {value}, bound {expect.bound}"

    @staticmethod
    def _newest_reading(
        readings: tuple[ModelRulingReading, ...], row_id: str
    ) -> ModelRulingReading | None:
        matches = [reading for reading in readings if reading.row_id == row_id]
        if not matches:
            return None
        return max(matches, key=lambda reading: reading.read_at)
