"""Golden-chain RED/GREEN tests for node_report_validation_compute (OMN-15163).

Every case drives the REAL handler end to end (``HandlerReportValidation().
handle(ModelReportValidationRequest(...))``), never a surrogate/mock of the
validation logic itself -- the anchor-probe I/O boundary is the only thing
faked (a hand-built ``ModelReportAnchorProbeResult``, exactly the typed shape
``node_report_anchor_probe_effect`` (OMN-15164) hands this node in production;
this node's handler is pure and never performs that I/O itself).

RED cases (one each, per the ticket brief):
- bare "Done." summary -> INVALID_SHAPE (the 2026-07-25 bare-acknowledgement
  incident this whole report-contract chain exists to close).
- every field filled with the literal "test" -> INVALID_SHAPE (the WORSE
  2026-07-25 incident: a shape-only checker let this "validate").
- anchor-context absent (no probe_result supplied) on a report that carries
  a content-anchor claim -> ANCHOR_UNCHECKABLE (fail-closed, never skipped).
- probe_result says a claimed sha did NOT resolve -> INVALID_CONTENT.

GREEN: one realistic report per REACHABLE dispatch_worker role (fixer, plus
all 5 roles that default to scout), each with fully-resolved anchor probes.

A dedicated ``ops``-role case proves the declared-out-of-scope dispatch role
fails closed too, rather than silently defaulting anywhere.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_dispatch_report_role import EnumDispatchReportRole

from omnimarket.events.report_anchor_probe import (
    EnumAnchorProbeStatus,
    ModelPathProbeResult,
    ModelReportAnchorProbeResult,
    ModelShaProbeResult,
)
from omnimarket.nodes.node_report_validation_compute.handlers.handler_report_validation import (
    HandlerReportValidation,
)
from omnimarket.nodes.node_report_validation_compute.models import (
    EnumDispatchWorkerRole,
    EnumReportValidationVerdict,
    ModelReportValidationRequest,
)

HEAD_SHA = "a" * 40
SUBSTANTIVE_SUMMARY_IMPLEMENTER = (
    "Built node_report_validation_compute per OMN-15163: def-B shape + "
    "content-anchor validation against the OMN-15161 report contract."
)

# Shared correlation_id for every RED/GREEN case that pairs a request with a
# probe_result: the handler discards a probe_result whose correlation_id
# does not match the request's (a probe computed for a different dispatch is
# untrusted context, per the CodeRabbit-flagged correlation-binding fix), so
# every fixture that means for its probe to actually apply must use this same
# id. `test_red_probe_result_wrong_correlation_is_anchor_uncheckable` below is
# the one deliberate exception.
CORRELATION_ID = uuid4()


def _handle(
    dispatch_role: EnumDispatchWorkerRole,
    raw_report_payload: dict[str, object],
    probe_result: ModelReportAnchorProbeResult | None = None,
    correlation_id: UUID = CORRELATION_ID,
):
    request = ModelReportValidationRequest(
        correlation_id=correlation_id,
        dispatch_role=dispatch_role,
        raw_report_payload=raw_report_payload,
        probe_result=probe_result,
    )
    return HandlerReportValidation().handle(request)


def _implementer_payload(*, summary: str, files_path: str) -> dict[str, object]:
    return {
        "role": "implementer",
        "pr_number": 1905,
        "branch": "jonah/omn-15163-report-validation-compute",
        "head_sha": HEAD_SHA,
        "verdict": "implemented",
        "files_changed_paths": [files_path],
        "summary": summary,
    }


def _resolved_implementer_probe(files_path: str) -> ModelReportAnchorProbeResult:
    return ModelReportAnchorProbeResult(
        correlation_id=CORRELATION_ID,
        sha_results=(
            ModelShaProbeResult(
                field_name="head_sha",
                sha=HEAD_SHA,
                status=EnumAnchorProbeStatus.RESOLVED,
                detail="resolves to a real commit",
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="files_changed_paths",
                path=files_path,
                resolved_path=f"/repo/{files_path}",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# RED
# ---------------------------------------------------------------------------


def test_red_bare_acknowledgement_summary_is_invalid_shape() -> None:
    payload = _implementer_payload(
        summary="Done.",
        files_path="src/omnimarket/nodes/node_report_validation_compute/__init__.py",
    )
    result = _handle(EnumDispatchWorkerRole.fixer, payload)

    assert result.verdict is EnumReportValidationVerdict.INVALID_SHAPE
    assert result.report_role is EnumDispatchReportRole.IMPLEMENTER
    assert any("summary" in v for v in result.violations)


def test_red_literal_test_fill_is_invalid_shape() -> None:
    """Reproduces the WORSE 2026-07-25 class: every field filled with 'test'."""
    payload = {
        "role": "scout",
        "verdict": "test",
        "findings_paths": ["test"],
        "summary": "test",
        "pr_number": None,
    }
    result = _handle(EnumDispatchWorkerRole.watcher, payload)

    assert result.verdict is EnumReportValidationVerdict.INVALID_SHAPE
    assert result.report_role is EnumDispatchReportRole.SCOUT
    # Both the invalid `verdict` enum value AND the placeholder `summary`
    # literal are caught -- a shape-only checker (the incident class) would
    # have let `findings_paths: ["test"]` and a well-typed-but-placeholder
    # payload straight through.
    assert any("verdict" in v for v in result.violations)
    assert any("summary" in v for v in result.violations)


def test_red_anchor_context_absent_is_anchor_uncheckable() -> None:
    payload = _implementer_payload(
        summary=SUBSTANTIVE_SUMMARY_IMPLEMENTER,
        files_path="src/omnimarket/nodes/node_report_validation_compute/contract.yaml",
    )
    result = _handle(EnumDispatchWorkerRole.fixer, payload, probe_result=None)

    assert result.verdict is EnumReportValidationVerdict.ANCHOR_UNCHECKABLE
    assert any("head_sha" in v and "missing_context" in v for v in result.violations)
    assert any(
        "files_changed_paths" in v and "missing_context" in v for v in result.violations
    )


def test_red_probe_says_sha_unresolved_is_invalid_content() -> None:
    files_path = "src/omnimarket/nodes/node_report_validation_compute/contract.yaml"
    payload = _implementer_payload(
        summary=SUBSTANTIVE_SUMMARY_IMPLEMENTER, files_path=files_path
    )
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=CORRELATION_ID,
        sha_results=(
            ModelShaProbeResult(
                field_name="head_sha",
                sha=HEAD_SHA,
                status=EnumAnchorProbeStatus.NOT_RESOLVED,
                detail="does not resolve to a real commit in git_dir",
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="files_changed_paths",
                path=files_path,
                resolved_path=f"/repo/{files_path}",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )
    result = _handle(EnumDispatchWorkerRole.fixer, payload, probe_result=probe_result)

    assert result.verdict is EnumReportValidationVerdict.INVALID_CONTENT
    assert any("head_sha" in v and "not_resolved" in v for v in result.violations)


def test_red_probe_result_for_a_different_sha_is_anchor_uncheckable() -> None:
    """A RESOLVED probe entry for the same field but a DIFFERENT sha must
    never satisfy this report's claim -- it is exactly as uninformative as no
    probe at all (CodeRabbit finding, PR #1911)."""
    files_path = "src/omnimarket/nodes/node_report_validation_compute/contract.yaml"
    payload = _implementer_payload(
        summary=SUBSTANTIVE_SUMMARY_IMPLEMENTER, files_path=files_path
    )
    stale_sha = "f" * 40
    assert stale_sha != HEAD_SHA
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=CORRELATION_ID,
        sha_results=(
            ModelShaProbeResult(
                field_name="head_sha",
                sha=stale_sha,
                status=EnumAnchorProbeStatus.RESOLVED,
                detail="resolves to a real commit -- but the WRONG one",
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="files_changed_paths",
                path=files_path,
                resolved_path=f"/repo/{files_path}",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )
    result = _handle(EnumDispatchWorkerRole.fixer, payload, probe_result=probe_result)

    assert result.verdict is EnumReportValidationVerdict.ANCHOR_UNCHECKABLE
    assert any("head_sha" in v and "missing_context" in v for v in result.violations)


def test_red_probe_result_wrong_correlation_is_anchor_uncheckable() -> None:
    """A probe_result computed for a DIFFERENT correlation_id is untrusted
    context and must be discarded entirely, never silently reused to satisfy
    this report's anchor claims (CodeRabbit finding, PR #1911)."""
    files_path = "src/omnimarket/nodes/node_report_validation_compute/contract.yaml"
    payload = _implementer_payload(
        summary=SUBSTANTIVE_SUMMARY_IMPLEMENTER, files_path=files_path
    )
    other_correlation_id = uuid4()
    assert other_correlation_id != CORRELATION_ID
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=other_correlation_id,
        sha_results=(
            ModelShaProbeResult(
                field_name="head_sha",
                sha=HEAD_SHA,
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
        path_results=(
            ModelPathProbeResult(
                field_name="files_changed_paths",
                path=files_path,
                resolved_path=f"/repo/{files_path}",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )
    result = _handle(
        EnumDispatchWorkerRole.fixer,
        payload,
        probe_result=probe_result,
        correlation_id=CORRELATION_ID,
    )

    assert result.verdict is EnumReportValidationVerdict.ANCHOR_UNCHECKABLE
    assert any("head_sha" in v and "missing_context" in v for v in result.violations)
    assert any(
        "files_changed_paths" in v and "missing_context" in v for v in result.violations
    )


def test_red_ops_role_is_declared_unmappable() -> None:
    result = _handle(EnumDispatchWorkerRole.ops, {"summary": "irrelevant"})

    assert result.verdict is EnumReportValidationVerdict.INVALID_SHAPE
    assert result.report_role is None
    assert any("ops" in v and "out-of-scope" in v for v in result.violations)


# ---------------------------------------------------------------------------
# GREEN -- one realistic report per reachable dispatch_worker role
# ---------------------------------------------------------------------------


def test_green_fixer_implementer_report_is_valid() -> None:
    files_path = "src/omnimarket/nodes/node_report_validation_compute/handlers/handler_report_validation.py"
    payload = _implementer_payload(
        summary=SUBSTANTIVE_SUMMARY_IMPLEMENTER, files_path=files_path
    )
    result = _handle(
        EnumDispatchWorkerRole.fixer,
        payload,
        probe_result=_resolved_implementer_probe(files_path),
    )

    assert result.verdict is EnumReportValidationVerdict.VALID
    assert result.report_role is EnumDispatchReportRole.IMPLEMENTER
    assert result.violations == ()


def test_result_echoes_the_request_correlation_id_and_dispatch_role() -> None:
    """The result is correlation-traceable back to its request: a caller
    fanning out many concurrent validations must be able to match each
    ModelReportValidationResult to the request that produced it."""
    correlation_id = uuid4()
    dispatch_role = EnumDispatchWorkerRole.sweep
    request = ModelReportValidationRequest(
        correlation_id=correlation_id,
        dispatch_role=dispatch_role,
        raw_report_payload={"summary": "irrelevant for this assertion"},
    )

    result = HandlerReportValidation().handle(request)

    assert result.correlation_id == correlation_id
    assert result.dispatch_role is dispatch_role


@pytest.mark.parametrize(
    ("dispatch_role", "findings_path", "summary"),
    [
        (
            EnumDispatchWorkerRole.watcher,
            "docs/diagnosis-omn-15163-ci-watch-2026-07-26.md",
            "PR #1905 CI green across all required contexts; no failing checks "
            "observed across the full 90-minute watch window.",
        ),
        (
            EnumDispatchWorkerRole.designer,
            "docs/design/omn-15163-report-validation-design.md",
            "Drafted the report-validation COMPUTE node design covering shape "
            "and content-anchor checks, converged after 2 hostile_reviewer rounds.",
        ),
        (
            EnumDispatchWorkerRole.auditor,
            "docs/diagnosis-omn-15163-contract-audit-2026-07-26.md",
            "Audited node_report_validation_compute's contract.yaml against "
            "the request/result models; found zero drift between declared "
            "inputs/outputs and the pydantic field set.",
        ),
        (
            EnumDispatchWorkerRole.synthesizer,
            "docs/design/omn-15163-synthesized.md",
            "Reconciled the OMN-15161/15163/15164 interface contracts into one "
            "doc; no cross-domain conflicts found between the report models "
            "and the anchor-probe seam.",
        ),
        (
            EnumDispatchWorkerRole.sweep,
            "docs/diagnosis-omn-15163-sweep-2026-07-26.md",
            "runtime_sweep :14 -> unwired_handlers=0 orphan_topics=0 total_nodes=312",
        ),
    ],
)
def test_green_scout_default_roles_are_valid(
    dispatch_role: EnumDispatchWorkerRole, findings_path: str, summary: str
) -> None:
    payload = {
        "role": "scout",
        "verdict": "found",
        "findings_paths": [findings_path],
        "summary": summary,
        "pr_number": None,
    }
    probe_result = ModelReportAnchorProbeResult(
        correlation_id=CORRELATION_ID,
        path_results=(
            ModelPathProbeResult(
                field_name="findings_paths",
                path=findings_path,
                resolved_path=f"/repo/{findings_path}",
                status=EnumAnchorProbeStatus.RESOLVED,
            ),
        ),
    )

    result = _handle(dispatch_role, payload, probe_result=probe_result)

    assert result.verdict is EnumReportValidationVerdict.VALID, result.violations
    assert result.report_role is EnumDispatchReportRole.SCOUT
    assert result.violations == ()
