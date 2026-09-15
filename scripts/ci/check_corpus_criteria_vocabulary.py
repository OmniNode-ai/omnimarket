#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a delegation corpus case whose criteria the boundary will not accept (OMN-18349).

The Layer-2 golden corpus publishes each integration case through the
delegate-skill command topic, and every criterion in that case's
``acceptance_criteria`` crosses the delegation boundary. The boundary declares
what it accepts in one place -- ``SUPPORTED_ACCEPTANCE_CRITERIA`` in
``omnimarket.models.delegation.wire.model_delegation_request`` -- and refuses the
whole request when a criterion is not in it.

The corpus never satisfied that. It was authored on 2026-06-23 (OMN-13540)
naming ``non_empty``, ``has_code_block``, ``min_length:N`` and ``contains:...``,
none of which was in the allowlist on that day or on any day since; the
boundary's spelling of the first has always been ``response_non_empty``. The
divergence was invisible for eighty-three days because the nightly died before
publishing a single case, so nothing ever carried a corpus criterion to the
boundary to be refused. The first run that did (34796339016, 2026-09-14) had all
nine cases refused with ``unsupported acceptance criteria``.

This checker is the mechanical half of that fix, and it makes both failure modes
red rather than invisible:

1. **Unaccepted vocabulary.** Each case's criteria are handed to the boundary's
   own ``validate_acceptance_criteria``. No second copy of the allowlist lives
   here, so a slug retired at the boundary turns this red on the next commit
   instead of on some later night.

2. **Vocabulary that is accepted and then asserts nothing.** Passing the
   boundary is necessary and not sufficient. Request criteria are merged into
   ``dod_deterministic`` (``handler_quality_gate.delta``), so an
   allowlisted-but-heuristic-only name -- the shape ``sub_tasks_verified`` had
   before OMN-15196 retired it -- reaches the gate as an unsupported
   DETERMINISTIC check and hard-fails the response as MALFORMED; and a check in
   ``_UNEVALUATED_DETERMINISTIC_CHECKS`` (``passes_existing_tests``) is dropped
   from the scored fraction entirely, so declaring it asserts nothing at all.
   Both are found by RUNNING each criterion through the real gate and reading its
   own per-rule receipt, rather than by keeping a list here of which names have
   executors -- a list is the thing that drifts.

Unit-layer rows are exempt and are checked for exactly that reason by
``tests/delegation_golden/test_corpus_boundary_conformance.py``, which pins that
the runner publishes the integration rows and only those, so the exemption
cannot silently widen into the published set.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnibase_core.models.delegation.wire import (  # noqa: E402
    ModelQualityGateInput,
)

from omnimarket.models.delegation.wire.model_delegation_request import (  # noqa: E402
    validate_acceptance_criteria,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (  # noqa: E402
    delta as evaluate_quality_gate,
)
from tests.delegation_golden.corpus_loader import (  # noqa: E402
    CORPUS_PATH,
    ModelCorpus,
    load_corpus,
)

# A fixed, non-empty probe. Content only has to be non-empty: this checker reads
# whether a criterion RESOLVED to an executor, never whether the probe passes it.
# Empty content would trip the gate's own empty-response floor and add a failure
# reason that belongs to the content rather than to the criterion under test.
_PROBE_CONTENT = "probe"
_PROBE_CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000000")

# The gate's message for a name with no deterministic executor. Matched on the
# stable prefix, so the criterion name it interpolates does not have to be
# reconstructed here.
_NO_EXECUTOR_PREFIX = "MALFORMED: unsupported deterministic DoD check"


def _resolution_problem(task_type: str, criterion: str) -> str | None:
    """Return why this criterion asserts nothing at the gate, or None if it does.

    ``replace_task_class`` makes ``dod_deterministic`` exactly the one criterion
    under test, so every rule evaluation in the result is attributable to it and
    to nothing the task class also declares.

    The gate's per-rule receipt (``rule_evaluations``, OMN-18295) is the reading
    used here because it is built where the gate knows which check produced which
    message: a criterion that RAN contributes exactly one evaluation named after
    itself, a criterion with no executor contributes one whose detail is the
    MALFORMED-unsupported message, and a criterion the gate skips contributes
    none at all -- "it did not run" and "it ran and passed" are distinguishable,
    which is the whole point of asking here rather than reading a pass/fail.
    """
    result = evaluate_quality_gate(
        ModelQualityGateInput(
            correlation_id=_PROBE_CORRELATION_ID,
            task_type=task_type,
            llm_response_content=_PROBE_CONTENT,
            dod_deterministic=(),
            dod_heuristic=(),
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(criterion,),
        )
    )
    evaluations = [e for e in result.rule_evaluations if e.rule == criterion]
    if not evaluations:
        return (
            "is allowlisted but the quality gate never evaluates it -- it "
            "contributes neither a pass nor a failure and is dropped from the "
            "scored fraction, so declaring it asserts nothing"
        )
    for evaluation in evaluations:
        detail = evaluation.detail or ""
        if detail.startswith(_NO_EXECUTOR_PREFIX):
            return (
                "is allowlisted but has no deterministic executor at the quality "
                "gate, so it hard-fails every response as MALFORMED: "
                f"{detail!r}"
            )
    return None


def check_corpus(corpus: ModelCorpus | None = None) -> list[str]:
    """Return every criterion violation in the published corpus (empty == clean)."""
    resolved = corpus if corpus is not None else load_corpus()
    violations: list[str] = []
    for case in resolved.integration_cases():
        try:
            validate_acceptance_criteria(case.acceptance_criteria)
        except ValueError as exc:
            violations.append(
                f"{case.id}: the delegation boundary refuses this case -- {exc}"
            )
            # The gate is unreachable for a request the boundary rejects, so the
            # per-criterion resolution check below would report on criteria that
            # never arrive. Report the refusal and move to the next case.
            continue
        for criterion in case.acceptance_criteria:
            problem = _resolution_problem(case.task_type, criterion)
            if problem is not None:
                violations.append(f"{case.id}: {criterion!r} {problem}")
    return violations


def main() -> int:
    violations = check_corpus()
    if violations:
        print(
            "The delegation golden corpus must speak the boundary's own "
            f"acceptance vocabulary ({CORPUS_PATH.name}, OMN-18349):"
        )
        for violation in violations:
            print(f"  - {violation}")
        print(
            "  Fix the corpus, never SUPPORTED_ACCEPTANCE_CRITERIA: the platform "
            "withdrew substring scoring deliberately, so a case that needs one "
            "is an xfail naming its ticket, not a wider allowlist."
        )
        return 1
    corpus = load_corpus()
    criteria = sum(len(c.acceptance_criteria) for c in corpus.integration_cases())
    print(
        f"delegation corpus criteria OK ({len(corpus.integration_cases())} "
        f"published case(s), {criteria} criterion instance(s) checked)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
