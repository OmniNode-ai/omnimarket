# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An open judge gate for tests of the emit mechanics that are not about the gate (OMN-23152).

The emit node's contract requires the judge's acceptance of every row. A test of typing, spooling or
topic fan-out feeds fixture rows no judge saw, so it injects this gate. The gate's own refusal is
proved in ``node_work_ledger_append_effect/tests/test_emit_only_after_judge_accepts.py`` against the
contract's real setting.
"""

from omnimarket.models.work_ledger_append.model_work_ledger_judge_acceptance import (
    ModelWorkLedgerJudgeGate,
)

OPEN_JUDGE_GATE = ModelWorkLedgerJudgeGate(
    require_acceptance=False, refusal_prefix="open-gate-for-emit-mechanics-tests"
)
