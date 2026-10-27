# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compaction replay scoring for plan task TA.9 (OMN-21111).

score_ruling_presence checks (a) every expected ruling's exact words in 100 percent
of cases. compare_answers checks (b) the package matches the expected next action
at least as often as the native answer and (c) two runs give identical candidate_ids
per case. size_review_rows measures (d) capacity: per-boundary estimates for
OPERATOR_WORDS_UNRECORDED and required factors, native-fallback share, and a
positive control when that share is zero; and (e) D10 sizing: the prompts and tokens
review rows would have to name, oldest first, or does not fit, and rows per session.

The caller builds each package through node_context_pack_orchestrator in pinned
mode and asks the model. This node only scores caller-supplied inputs. It never
removes a prompt itself or recommends an action, and performs no I/O.
"""

from __future__ import annotations

from omnimarket.nodes.node_context_replay_eval_compute.models import (
    EnumAnswerGrade,
    EnumPresence,
    EnumSizingVerdict,
    ModelAnswerComparisonRequest,
    ModelAnswerComparisonResult,
    ModelBoundaryCarry,
    ModelBoundarySizing,
    ModelCasePresence,
    ModelReviewRowSizingRequest,
    ModelReviewRowSizingResult,
    ModelRulingPresenceRequest,
    ModelRulingPresenceResult,
    ModelRunAnswerCounts,
    ModelSessionSizing,
    ModelStatementPresence,
)

_QUOTE_FOLD = str.maketrans(
    {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'}
)

__all__ = ["HandlerAnswerComparison", "HandlerReviewRowSizing", "HandlerRulingPresence"]


class HandlerRulingPresence:
    """Check exact quotes or spans, folding only curly quotes; fail closed on no words."""

    def handle(self, request: ModelRulingPresenceRequest) -> ModelRulingPresenceResult:
        cases: list[ModelCasePresence] = []
        falsifiers: list[str] = []
        for case in request.cases:
            text = case.package_text.translate(_QUOTE_FOLD)
            statements: list[ModelStatementPresence] = []
            for expected in case.expected:
                if expected.quote is not None:
                    present = expected.quote.translate(_QUOTE_FOLD) in text
                elif expected.required_spans:
                    present = all(
                        span.translate(_QUOTE_FOLD) in text
                        for span in expected.required_spans
                    )
                else:
                    statements.append(
                        ModelStatementPresence(
                            statement_id=expected.statement_id,
                            presence=EnumPresence.UNSCORABLE,
                        )
                    )
                    falsifiers.append(
                        f"case {case.case_id}: statement {expected.statement_id} {EnumPresence.UNSCORABLE}"
                    )
                    continue
                presence = EnumPresence.PRESENT if present else EnumPresence.MISSING
                statements.append(
                    ModelStatementPresence(
                        statement_id=expected.statement_id, presence=presence
                    )
                )
                if presence is not EnumPresence.PRESENT:
                    falsifiers.append(
                        f"case {case.case_id}: statement {expected.statement_id} {presence}"
                    )
            cases.append(
                ModelCasePresence(
                    case_id=case.case_id,
                    candidate_id=case.candidate_id,
                    statements=tuple(statements),
                    passed=all(
                        statement.presence is EnumPresence.PRESENT
                        for statement in statements
                    ),
                )
            )
        passed = sum(case.passed for case in cases)
        return ModelRulingPresenceResult(
            cases=tuple(cases),
            cases_total=len(cases),
            cases_passed=passed,
            all_present=passed == len(cases),
            falsifiers=tuple(falsifiers),
        )


class HandlerAnswerComparison:
    """Compare measured answers; absent answers cannot establish quality parity."""

    def handle(
        self, request: ModelAnswerComparisonRequest
    ) -> ModelAnswerComparisonResult:
        counts: list[ModelRunAnswerCounts] = []
        falsifiers: list[str] = []
        identities: dict[str, set[str]] = {}
        appearances: dict[str, int] = {}
        for run in request.runs:
            package = sum(
                case.package_answer is EnumAnswerGrade.MATCH for case in run.cases
            )
            native = sum(
                case.native_answer is EnumAnswerGrade.MATCH for case in run.cases
            )
            native_unavailable = sum(
                case.native_answer is EnumAnswerGrade.UNAVAILABLE for case in run.cases
            )
            package_unavailable = sum(
                case.package_answer is EnumAnswerGrade.UNAVAILABLE for case in run.cases
            )
            counts.append(
                ModelRunAnswerCounts(
                    run_id=run.run_id,
                    cases=len(run.cases),
                    package_matches=package,
                    native_matches=native,
                    native_unavailable=native_unavailable,
                    package_unavailable=package_unavailable,
                    package_at_least_native=(
                        package >= native
                        and package_unavailable == 0
                        and native_unavailable == 0
                    ),
                )
            )
            if package < native:
                falsifiers.append(
                    f"run {run.run_id}: package {package} < native {native}"
                )
            if counts[-1].package_unavailable or counts[-1].native_unavailable:
                falsifiers.append(
                    f"run {run.run_id}: answers unavailable: "
                    f"package {counts[-1].package_unavailable}, "
                    f"native {counts[-1].native_unavailable}"
                )
            for case in run.cases:
                identities.setdefault(case.case_id, set()).add(case.candidate_id)
                appearances[case.case_id] = appearances.get(case.case_id, 0) + 1
        mismatches = tuple(
            sorted(
                case_id
                for case_id, ids in identities.items()
                if len(ids) > 1 or appearances[case_id] != len(request.runs)
            )
        )
        falsifiers.extend(
            f"case {case_id}: candidate_id differs across runs"
            for case_id in mismatches
        )
        checked = len(request.runs) >= 2
        if not checked:
            falsifiers.append("determinism not checked: fewer than two runs")
        return ModelAnswerComparisonResult(
            runs=tuple(counts),
            next_action_met=all(
                run.package_at_least_native
                and run.package_unavailable == 0
                and run.native_unavailable == 0
                for run in counts
            ),
            determinism_checked=checked,
            candidate_id_mismatches=mismatches,
            candidate_ids_identical=checked and not mismatches,
            falsifiers=tuple(falsifiers),
        )


def _size_boundary(
    boundary: ModelBoundaryCarry, default_budget: int
) -> ModelBoundarySizing:
    budget = boundary.budget_override_tokens or default_budget
    operator = boundary.operator_words_overhead_tokens + sum(
        prompt.token_estimate for prompt in boundary.carried_prompts
    )
    required = sum(boundary.other_required_tokens.values()) + operator
    exceeds = required > budget
    verdict = EnumSizingVerdict.FITS_AS_IS
    named: tuple[str, ...] = ()
    prompts_to_name: int | None = 0
    tokens_to_name: int | None = 0
    after: int | None = required
    if exceeds:
        prefix: list[str] = []
        removed = 0
        for prompt in sorted(
            boundary.carried_prompts,
            key=lambda prompt: (prompt.captured_at, prompt.prompt_id),
        ):
            prefix.append(prompt.prompt_id)
            removed += prompt.token_estimate
            if required - removed <= budget:
                break
        if required - removed <= budget:
            verdict = EnumSizingVerdict.FITS_AFTER_REVIEW
            named = tuple(prefix)
            prompts_to_name = len(prefix)
            tokens_to_name = removed
            after = required - removed
        else:
            verdict = EnumSizingVerdict.DOES_NOT_FIT
            prompts_to_name = None
            tokens_to_name = None
            after = None

    # Rebuild from the input set, independently of prefix token subtraction.
    named_ids = set(named)
    carried_ids = {prompt.prompt_id for prompt in boundary.carried_prompts}
    remaining = tuple(
        prompt
        for prompt in boundary.carried_prompts
        if prompt.prompt_id not in named_ids
    )
    replay_tokens = (
        sum(boundary.other_required_tokens.values())
        + boundary.operator_words_overhead_tokens
        + sum(prompt.token_estimate for prompt in remaining)
    )
    if verdict is EnumSizingVerdict.DOES_NOT_FIT:
        replay_reproduces = (
            sum(boundary.other_required_tokens.values())
            + boundary.operator_words_overhead_tokens
            > budget
        )
    else:
        replay_reproduces = (
            named_ids <= carried_ids
            and replay_tokens == after
            and replay_tokens <= budget
        )
    return ModelBoundarySizing(
        session_id=boundary.session_id,
        boundary_id=boundary.boundary_id,
        boundary_at=boundary.boundary_at,
        control=boundary.control,
        budget_tokens=budget,
        operator_words_tokens=operator,
        required_tokens=required,
        exceeds_budget=exceeds,
        verdict=verdict,
        prompts_to_name=prompts_to_name,
        tokens_to_name=tokens_to_name,
        named_prompt_ids=named,
        required_tokens_after_review=after,
        replay_reproduces=replay_reproduces,
    )


class HandlerReviewRowSizing:
    """Size hypothetical review rows without changing the carried prompts."""

    def handle(
        self, request: ModelReviewRowSizingRequest
    ) -> ModelReviewRowSizingResult:
        boundaries = tuple(
            _size_boundary(boundary, request.budget_tokens)
            for boundary in request.boundaries
        )
        falsifiers = [
            f"boundary {boundary.session_id}/{boundary.boundary_id}: replay does not reproduce {boundary.verdict}"
            for boundary in boundaries
            if not boundary.replay_reproduces
        ]
        by_session: dict[str, list[ModelBoundarySizing]] = {}
        for boundary in boundaries:
            if not boundary.control:
                by_session.setdefault(boundary.session_id, []).append(boundary)
        sessions: list[ModelSessionSizing] = []
        for session_id, session_boundaries in sorted(by_session.items()):
            named: set[str] = set()
            rows = 0
            for boundary in sorted(
                session_boundaries,
                key=lambda boundary: (boundary.boundary_at, boundary.boundary_id),
            ):
                if set(boundary.named_prompt_ids) - named:
                    rows += 1
                named.update(boundary.named_prompt_ids)
            sessions.append(
                ModelSessionSizing(
                    session_id=session_id,
                    boundaries=len(session_boundaries),
                    exceeding=sum(
                        boundary.exceeds_budget for boundary in session_boundaries
                    ),
                    does_not_fit=sum(
                        boundary.verdict is EnumSizingVerdict.DOES_NOT_FIT
                        for boundary in session_boundaries
                    ),
                    review_rows=rows,
                )
            )
        total = sum(session.boundaries for session in sessions)
        exceeding = sum(session.exceeding for session in sessions)
        does_not_fit = sum(session.does_not_fit for session in sessions)
        controls = tuple(boundary for boundary in boundaries if boundary.control)
        control_exceeding = sum(boundary.exceeds_budget for boundary in controls)
        has_control = control_exceeding > 0 if exceeding == 0 else True
        if not has_control:
            falsifiers.append("zero fallback frequency without a positive control")
        return ModelReviewRowSizingResult(
            boundaries=boundaries,
            sessions=tuple(sessions),
            boundaries_total=total,
            exceeding=exceeding,
            fallback_share=round(exceeding / total, 4) if total else 0.0,
            does_not_fit=does_not_fit,
            does_not_fit_share=round(does_not_fit / total, 4) if total else 0.0,
            control_boundaries=len(controls),
            control_exceeding=control_exceeding,
            zero_fallback_has_control=has_control,
            falsifiers=tuple(falsifiers),
        )
