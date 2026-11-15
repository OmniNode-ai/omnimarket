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
* After the policy rows' verdicts, every refused row of the policy reads one
  UNREAD verdict carrying its refusal reason; refused rows count as unread and
  refuse the register.
* No verdict cell ever carries a read's raw output: the detail is one printable
  line built without the stdout, and ``one_line`` folds the row's own cells and
  the detail to the policy's ``max_detail_chars``, so a read's output can forge
  no tick line and leak through none.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    EnumRulingExpect,
    EnumRulingVerdict,
    ModelRulingReading,
    ModelRulingRefusedRow,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
    ModelRulingRow,
    ModelRulingVerdict,
)


def one_line(text: str, limit: int) -> str:
    """The one printable line of ``text``, cut to ``limit`` characters.

    Every run of whitespace or control characters becomes one space and the result is
    stripped, so a cell that came from a host's output or an overlay's row can carry
    no second tick line.
    """
    pieces: list[str] = []
    for char in text:
        if char.isprintable() and not char.isspace():
            pieces.append(char)
        elif pieces and pieces[-1] != " ":
            pieces.append(" ")
    return "".join(pieces).strip()[:limit]


class HandlerRulingRegister:
    """Judge the ruling register's readings against the policy; touch nothing."""

    def handle(self, request: ModelRulingRegisterRequest) -> ModelRulingRegisterResult:
        verdicts = tuple(self._judge_row(row, request) for row in request.policy.rows)
        verdicts += tuple(
            self._judge_refused_row(refused, request)
            for refused in request.policy.refused_rows
        )
        counts = Counter(verdict.verdict for verdict in verdicts)
        return ModelRulingRegisterResult(
            correlation_id=request.correlation_id,
            passed=counts[EnumRulingVerdict.TRUE] == len(verdicts)
            and not request.policy.refused_rows,
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

        limit = request.policy.max_detail_chars
        must_be_true = one_line(row.must_be_true, limit)
        owner = one_line(row.owner, limit)
        fix = one_line(row.fix, limit)
        detail = one_line(detail, limit)
        if verdict is EnumRulingVerdict.TRUE:
            why = must_be_true
        else:
            why = (
                f"ruling {row.ruling} not running: {must_be_true} "
                f"({verdict.value}: {detail}); owner {owner}"
            )
        return ModelRulingVerdict(
            row_id=row.row_id,
            ruling=row.ruling,
            owner=owner,
            verdict=verdict,
            detail=detail,
            why=why,
            fix=fix,
        )

    def _judge_refused_row(
        self, refused: ModelRulingRefusedRow, request: ModelRulingRegisterRequest
    ) -> ModelRulingVerdict:
        """One refused row's verdict: UNREAD with its refusal reason.

        The reason is built from validation error fields and messages in the policy, so no
        raw input value can enter a cell; ``one_line`` folds every cell to the policy's
        ``max_detail_chars`` anyway. A refused row counts as unread and never TRUE.
        """
        limit = request.policy.max_detail_chars
        return ModelRulingVerdict(
            row_id=refused.row_id,
            ruling=refused.ruling,
            owner=one_line(refused.owner, limit),
            verdict=EnumRulingVerdict.UNREAD,
            detail=one_line(refused.reason, limit),
            why=one_line(f"register row refused: {refused.reason}", limit),
            fix="correct this row in the node_ruling_register_compute overlay",
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
            stripped = reading.stdout.strip()
            if stripped:
                # The output itself never enters the detail: it would carry a tick
                # line's separators and any host's bytes into stdout.
                return (
                    EnumRulingVerdict.FALSE,
                    f"stdout is not empty ({len(stripped)} characters)",
                )
            return EnumRulingVerdict.TRUE, "holds"
        if expect.kind is EnumRulingExpect.STDOUT_EQUALS:
            if reading.stdout.strip() == expect.value:
                return EnumRulingVerdict.TRUE, "holds"
            return EnumRulingVerdict.FALSE, "stdout is not the expected value"
        if expect.bound is None:
            return EnumRulingVerdict.UNREAD, "int_at_most names no bound"
        try:
            value = int(reading.stdout.strip())
        except ValueError:
            return EnumRulingVerdict.UNREAD, "answer is not an integer"
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
