# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-path refusal drill's core on the Postgres gate store: a planted bypass is refused.

Needs the integration database (a runner without one skips under the repository's silent-skip
guard); the relations are the gate store node's migrations, applied by the shared fixture.
"""

from __future__ import annotations

import itertools

import pytest

from omnimarket.models.model_start_admission import (
    EnumAdmissionDecision,
    EnumAdmissionOperation,
    ModelStartAdmissionRequest,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
    InMemoryAdmissionLog,
)
from tests.nodes.node_gate_store_effect.contract_suite import POLICY
from tests.nodes.node_gate_store_effect.test_postgres_store import (  # noqa: F401
    _database,
    postgres_factory,
)
from tests.nodes.node_start_admission_effect.test_start_admission import POLICY_ADMIT

pytestmark = pytest.mark.integration

OP = EnumAdmissionOperation


_attempts = itertools.count()


def ask(h: HandlerStartAdmission, op: OP, start: str, path: str = "enforced_path"):
    """One new attempt: each call has its own correlation id, as a new start has."""
    return h.handle(
        ModelStartAdmissionRequest(
            correlation_id=f"{op.value}-{start}-{next(_attempts)}",
            operation=op,
            path=path,
            start_id=start,
            piece="gamma",
        )
    )


def drill() -> tuple[HandlerStartAdmission, InMemoryAdmissionLog]:
    log = InMemoryAdmissionLog()
    return HandlerStartAdmission(
        postgres_factory()(POLICY, None), POLICY_ADMIT, log
    ), log


def test_admit_start_single_use_limit_and_planted_bypass_on_postgres() -> None:
    h, log = drill()
    assert ask(h, OP.ADMIT, "s1").decision is EnumAdmissionDecision.ADMITTED
    assert ask(h, OP.START, "s1").decision is EnumAdmissionDecision.ADMITTED
    second = ask(h, OP.START, "s1")
    assert (second.decision, second.store_outcome) == (
        EnumAdmissionDecision.REFUSED,
        "refused_consumed",
    )
    over = ask(
        h, OP.ADMIT, "s2"
    )  # gamma's limit is 1 and s1 is consumed, so it still counts
    assert (over.decision, over.store_outcome) == (
        EnumAdmissionDecision.REFUSED,
        "refused_limit",
    )
    bypass = ask(h, OP.START, "planted-bypass")
    assert (bypass.decision, bypass.store_outcome) == (
        EnumAdmissionDecision.REFUSED,
        "refused_unknown_reservation",
    )
    observed = ask(h, OP.START, "planted-bypass", path="observed_path")
    assert observed.decision is EnumAdmissionDecision.ADMITTED_OBSERVED
    assert [e.would_refuse for e in log.entries][-3:] == [True, True, True]
