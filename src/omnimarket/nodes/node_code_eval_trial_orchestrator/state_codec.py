# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared ``state_io`` codec of node_code_eval_trial_orchestrator.

omnibase_infra's state_io wiring resolves this class from the contract's
``state_io.codec`` block, loads the row keyed by the run id before
``handle()``, and after it calls :meth:`StateIoCodec.flush` to read back the
row the handler wrote, which it persists under its own compare-and-set (the
delegation orchestrator's pattern, OMN-14208). A lost race re-runs the leg.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalControlReading,
    ModelCodeEvalRunClosed,
    ModelCodeEvalSequenceReading,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_row import (
    ModelCodeEvalRunRow,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.row_store import (
    StateIoCodeEvalRowStore,
    decode_row,
    encode_row,
)

_STATE_IO_STORE = StateIoCodeEvalRowStore()


def shared_state_io_store() -> StateIoCodeEvalRowStore:
    """The one store the handler writes to and the codec flushes from."""
    return _STATE_IO_STORE


class StateIoCodec:
    """encode, decode, the post-handle flush bridge and the abandoned terminal."""

    def encode(self, state: ModelCodeEvalRunRow) -> bytes:
        return encode_row(state).encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelCodeEvalRunRow:
        return decode_row(raw)

    def flush(self, cid: str) -> str | None:
        """The row the handler wrote for ``cid`` this dispatch, or None."""
        return _STATE_IO_STORE.flush(cid)

    def build_abandoned_terminal(
        self,
        *,
        correlation_id: str,
        tenant_id: str,
        state: str,
        payload_json: str,
        failure_class: str,
        failure_code: str | None,
        max_wall_seconds: int,
    ) -> tuple[str, str, dict[str, object]] | None:
        """Build the terminal closed event for a row past its completion bound.

        Called by omnibase_infra's state_io wiring for a row that is still
        ``in_flight`` and not closed. Infra owns the bus, the envelope id and
        the topic; this method owns the only thing infra cannot know -- the
        business shape of THIS node's terminal. The values are the row's own
        rather than invented: the request's identity (correlation id, tenant,
        harness revision and publish cells), the frozen digest and excluded
        counts, the settings' attempt budgets, and the control and sequence
        readings the run had reached. A control that never ran reads
        ``none``; a control's ``held`` is true exactly when its verdict is
        pass, and ``could_not_be_run`` when the verdict is unscorable; a
        sequence's band is its task row's.

        Returns ``None`` when the row's payload cannot be decoded into a run
        row at all (a shape this build does not understand is left alone
        rather than closed out on a guess, and the sweep retries it after the
        next deploy) or when the row is already closed.
        """
        try:
            row = decode_row(payload_json)
        except (ValueError, ValidationError):
            return None
        if row.closed:
            return None
        controls = tuple(
            ModelCodeEvalControlReading(
                task_key=task.task_key,
                verdict=task.control.label if task.control is not None else "none",
                held=(
                    task.control is not None
                    and task.control.verdict is EnumCodeScoreVerdict.PASS
                ),
                could_not_be_run=(
                    task.control is not None
                    and task.control.verdict.value.startswith("unscorable")
                ),
            )
            for task in row.tasks
        )
        bands = {task.task_key: task.band for task in row.tasks}
        sequences = tuple(
            ModelCodeEvalSequenceReading(
                task_key=sequence.task_key,
                model=sequence.model,
                backend_id=sequence.backend_id,
                band=bands[sequence.task_key],
                verdicts=tuple(verdict.label for verdict in sequence.verdicts),
                duration_s=sequence.duration_s,
            )
            for sequence in row.sequences
        )
        closed = ModelCodeEvalRunClosed(
            correlation_id=row.request.correlation_id,
            tenant_id=row.request.tenant_id,
            harness_revision=row.request.harness_revision,
            publish_cells=row.request.publish_cells,
            attempt_budgets=row.settings.attempt_budgets,
            digest=row.digest,
            closed_at=datetime.now(UTC),
            abandoned=True,
            excluded_counts=row.excluded_counts,
            controls=controls,
            sequences=sequences,
        )
        return (
            "omnimarket.nodes.node_code_eval_trial_orchestrator.models",
            "ModelCodeEvalRunClosed",
            closed.model_dump(mode="json"),
        )


__all__: list[str] = ["StateIoCodec", "shared_state_io_store"]
