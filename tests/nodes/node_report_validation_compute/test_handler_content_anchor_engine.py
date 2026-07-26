"""Direct coverage of the shape/anchor-check engine for report roles that are
currently UNREACHABLE via node_dispatch_worker's 7-role vocabulary (verifier,
lander) -- see model_role_mapping.py: no dispatch_worker role maps to either.

The engine itself (``ROLE_TO_MODEL`` shape validation +
``_check_content_anchors``) is role-agnostic; these tests prove it handles
all 4 omnibase_core report shapes correctly, not just the 2 currently
reachable through ``ModelReportValidationRequest.dispatch_role``. Also covers
the best-effort (non-fail-closed) ``pr_number`` cross-check, which is outside
the ``*_sha``/``*_paths`` content-anchor naming convention by design.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.enums.enum_dispatch_report_verdict import (
    EnumDispatchReportLanderVerdict,
    EnumDispatchReportVerifierVerdict,
)
from omnibase_core.models.dispatch.report import (
    ModelDispatchReportLander,
    ModelDispatchReportVerifier,
)

from omnimarket.events.report_anchor_probe import (
    EnumAnchorProbeStatus,
    ModelPathProbeResult,
    ModelPrProbeResult,
    ModelReportAnchorProbeResult,
    ModelShaProbeResult,
)
from omnimarket.nodes.node_report_validation_compute.handlers.handler_report_validation import (
    _check_content_anchors,
)

VERIFIED_SHA = "c" * 40
MERGE_SHA = "d" * 40


def test_verifier_report_anchor_check_resolves_clean() -> None:
    report = ModelDispatchReportVerifier(
        pr_number=1905,
        verified_sha=VERIFIED_SHA,
        verdict=EnumDispatchReportVerifierVerdict.CONFIRMED,
        evidence_paths=["tests/test_golden_chain_report_validation_compute.py"],
        summary=(
            "Re-checked the implementer's claim against live CI: PR #1905 "
            "green, head_sha matches, all golden-chain tests pass."
        ),
    )
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=uuid4(),
        sha_results=(
            ModelShaProbeResult(
                field_name="verified_sha",
                sha=VERIFIED_SHA,
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="evidence_paths",
                path="tests/test_golden_chain_report_validation_compute.py",
                resolved_path="/repo/tests/test_golden_chain_report_validation_compute.py",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )

    missing, unresolved = _check_content_anchors(report, probe_result)

    assert missing == ()
    assert unresolved == ()


def test_lander_report_with_no_merge_needs_no_anchor_context() -> None:
    """merge_sha is conditionally required (only when verdict is MERGED); a
    BLOCKED land report carries no anchor claim at all, so a None probe_result
    is correctly NOT a violation."""
    report = ModelDispatchReportLander(
        pr_number=1905,
        merge_sha=None,
        verdict=EnumDispatchReportLanderVerdict.BLOCKED,
        summary=(
            "Land attempt blocked: CodeRabbit flagged 2 unresolved MAJOR "
            "threads on PR #1905; deferring to the implementer for a fix."
        ),
    )

    missing, unresolved = _check_content_anchors(report, None)

    assert missing == ()
    assert unresolved == ()


def test_lander_merged_report_anchor_context_absent_is_missing() -> None:
    report = ModelDispatchReportLander(
        pr_number=1905,
        merge_sha=MERGE_SHA,
        verdict=EnumDispatchReportLanderVerdict.MERGED,
        summary="Squash-merged PR #1905 onto dev after CI went green.",
    )

    missing, unresolved = _check_content_anchors(report, None)

    assert any("merge_sha" in v for v in missing)
    assert unresolved == ()


def test_pr_number_best_effort_check_flags_unresolved_match() -> None:
    report = ModelDispatchReportVerifier(
        pr_number=1905,
        verified_sha=VERIFIED_SHA,
        verdict=EnumDispatchReportVerifierVerdict.CONFIRMED,
        evidence_paths=["tests/test_golden_chain_report_validation_compute.py"],
        summary=(
            "Re-checked the implementer's claim against live CI: PR #1905 "
            "green, head_sha matches, all golden-chain tests pass."
        ),
    )
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=uuid4(),
        sha_results=(
            ModelShaProbeResult(
                field_name="verified_sha",
                sha=VERIFIED_SHA,
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="evidence_paths",
                path="tests/test_golden_chain_report_validation_compute.py",
                resolved_path="/repo/tests/test_golden_chain_report_validation_compute.py",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        pr_result=ModelPrProbeResult(
            field_name="pr_number",
            pr_number=1905,
            repo="OmniNode-ai/omnimarket",
            status=EnumAnchorProbeStatus.NOT_RESOLVED,
            detail="gh pr view: PR not found",
        ),
    )

    missing, unresolved = _check_content_anchors(report, probe_result)

    assert missing == ()
    assert any("pr_number" in v and "not_resolved" in v for v in unresolved)


def test_pr_number_with_no_pr_result_is_not_fail_closed() -> None:
    """pr_number is NOT a `_sha`/`_paths`-suffixed field, so an absent
    pr_result is best-effort, not ANCHOR_UNCHECKABLE, by design."""
    report = ModelDispatchReportVerifier(
        pr_number=1905,
        verified_sha=VERIFIED_SHA,
        verdict=EnumDispatchReportVerifierVerdict.CONFIRMED,
        evidence_paths=["tests/test_golden_chain_report_validation_compute.py"],
        summary=(
            "Re-checked the implementer's claim against live CI: PR #1905 "
            "green, head_sha matches, all golden-chain tests pass."
        ),
    )
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=uuid4(),
        sha_results=(
            ModelShaProbeResult(
                field_name="verified_sha",
                sha=VERIFIED_SHA,
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="evidence_paths",
                path="tests/test_golden_chain_report_validation_compute.py",
                resolved_path="/repo/tests/test_golden_chain_report_validation_compute.py",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        pr_result=None,
    )

    missing, unresolved = _check_content_anchors(report, probe_result)

    assert missing == ()
    assert unresolved == ()
