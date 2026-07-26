# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReportValidation — deterministic dispatch-report validator (OMN-15163).

Pure COMPUTE: no subprocess calls, no filesystem reads, no network, no model
calls. Two independent checks, per the ticket brief ("worker result ->
validate (shape AND content anchors)"):

1. Shape: does ``raw_report_payload`` construct into the role-resolved
   ``omnibase_core.models.dispatch.report`` model (OMN-15161)? This model is
   ``frozen=True, extra="forbid", strict=True`` and its ``summary`` field
   carries a ``field_validator`` that rejects placeholder/bare-acknowledgement
   literals (``validate_substantive_report_text`` -- the 2026-07-25
   literal-``"test"``-passed-validation incident this whole contract chain
   exists to close). Any pydantic ``ValidationError`` here -- whether a type
   mismatch or a field_validator content rejection -- is ``INVALID_SHAPE``.
2. Content anchors: for every ``*_sha``/``*_paths`` field the CONSTRUCTED
   report actually carries a value for, is there a matching, RESOLVED entry
   in the caller-supplied ``probe_result`` (OMN-15164 EFFECT node output),
   for the EXACT claimed sha/path -- a resolved probe entry for the same
   field but a DIFFERENT sha/path never satisfies the claim -- and for the
   SAME ``correlation_id`` as this request (a probe result computed for a
   different dispatch is untrusted, never silently reused)? A present anchor
   claim with no matching probe-result entry is ``ANCHOR_UNCHECKABLE``
   (fail-closed -- an unchecked anchor is never a silent pass); a present,
   checked, but NOT-resolved claim is ``INVALID_CONTENT``.

All I/O that produces ``probe_result`` already happened upstream in
``node_report_anchor_probe_effect`` (OMN-15164) -- this handler never calls
``omnibase_core.validation.validator_dispatch_report_anchors`` directly (that
module shells out to ``git``/reads the filesystem), which would make this
handler impure.
"""

from __future__ import annotations

import json

from omnibase_core.models.dispatch.report import ROLE_TO_MODEL
from pydantic import BaseModel, ValidationError

# SEAM IMPORT: see model_report_validation_request.py's comment for why this
# is omnimarket.events.report_anchor_probe (the canonical OWNER of the
# OMN-15164 EFFECT node's output shape), not a
# omnimarket.nodes.node_report_anchor_probe_effect reach-in.
from omnimarket.events.report_anchor_probe import (
    EnumAnchorProbeStatus,
    ModelPathProbeResult,
    ModelReportAnchorProbeResult,
    ModelShaProbeResult,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_request import (
    ModelReportValidationRequest,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_result import (
    ModelReportValidationResult,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_verdict import (
    EnumReportValidationVerdict,
)
from omnimarket.nodes.node_report_validation_compute.models.model_role_mapping import (
    resolve_report_role,
)

_SHA_SUFFIX = "_sha"
_PATHS_SUFFIX = "_paths"


def _shape_violations_from_error(exc: ValidationError) -> tuple[str, ...]:
    violations: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        violations.append(f"shape: {loc}: {error['msg']}")
    return tuple(violations)


def _index_sha_results(
    probe_result: ModelReportAnchorProbeResult | None,
) -> dict[tuple[str, str], ModelShaProbeResult]:
    """Index by ``(field_name, sha)``, not ``field_name`` alone -- a RESOLVED
    probe result for the same field but a DIFFERENT sha must never satisfy
    this report's claim (a stale or mismatched probe would otherwise pass)."""
    if probe_result is None:
        return {}
    return {
        (result.field_name, result.sha): result for result in probe_result.sha_results
    }


def _index_path_results(
    probe_result: ModelReportAnchorProbeResult | None,
) -> dict[tuple[str, str], ModelPathProbeResult]:
    if probe_result is None:
        return {}
    return {
        (result.field_name, result.path): result for result in probe_result.path_results
    }


def _check_content_anchors(
    validated_report: BaseModel,
    probe_result: ModelReportAnchorProbeResult | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(missing_context_violations, unresolved_violations)``.

    Walks every field on the VALIDATED report (not the raw payload) so the
    suffix convention is checked against real, typed values -- a ``*_paths``
    field is always a ``list[str]`` here, never a raw dict entry of unknown
    shape.
    """
    missing_context: list[str] = []
    unresolved: list[str] = []

    sha_by_field = _index_sha_results(probe_result)
    path_by_key = _index_path_results(probe_result)

    for field_name in type(validated_report).model_fields:
        value = getattr(validated_report, field_name)

        if field_name.endswith(_SHA_SUFFIX):
            if value is None:
                continue
            sha_result = sha_by_field.get((field_name, value))
            if sha_result is None:
                missing_context.append(f"anchor_missing_context: {field_name}")
            elif sha_result.status is not EnumAnchorProbeStatus.RESOLVED:
                unresolved.append(
                    f"anchor_unresolved: {field_name} "
                    f"({sha_result.status.value}: {sha_result.detail})"
                )

        elif field_name.endswith(_PATHS_SUFFIX):
            if not value:
                continue
            for path in value:
                path_result = path_by_key.get((field_name, path))
                if path_result is None:
                    missing_context.append(
                        f"anchor_missing_context: {field_name}[{path!r}]"
                    )
                elif path_result.status is not EnumAnchorProbeStatus.RESOLVED:
                    unresolved.append(
                        f"anchor_unresolved: {field_name}[{path!r}] "
                        f"({path_result.status.value}: {path_result.detail})"
                    )

    # pr_number is NOT a `_sha`/`_paths`-suffixed field, so it falls outside
    # omnibase_core's content-anchor naming convention (and this validator's
    # fail-closed ANCHOR_UNCHECKABLE rule applies only to that convention).
    # It is still cross-checked opportunistically when the caller supplied a
    # pr_result for the SAME pr_number: a resolved probe with a non-RESOLVED
    # status is real evidence the claim is wrong and must not be ignored. No
    # pr_result, or a pr_result for a DIFFERENT pr_number, is not itself a
    # failure -- pr_number confirmation stays best-effort by design.
    pr_number = getattr(validated_report, "pr_number", None)
    if pr_number is not None and probe_result is not None and probe_result.pr_result:
        pr_result = probe_result.pr_result
        if (
            pr_result.pr_number == pr_number
            and pr_result.status is not EnumAnchorProbeStatus.RESOLVED
        ):
            unresolved.append(
                f"anchor_unresolved: pr_number ({pr_result.status.value}: {pr_result.detail})"
            )

    return tuple(missing_context), tuple(unresolved)


class HandlerReportValidation:
    """Deterministically validate one dispatch-worker report payload."""

    def handle(
        self, request: ModelReportValidationRequest
    ) -> ModelReportValidationResult:
        report_role = resolve_report_role(request.dispatch_role)

        if report_role is None:
            return ModelReportValidationResult(
                correlation_id=request.correlation_id,
                dispatch_role=request.dispatch_role,
                report_role=None,
                verdict=EnumReportValidationVerdict.INVALID_SHAPE,
                violations=(
                    f"shape: dispatch_role: {request.dispatch_role.value!r} has no "
                    "mapped report_role (declared out-of-scope -- see "
                    "model_role_mapping.ROLE_MAPPING_TABLE / UNMAPPABLE_DISPATCH_ROLES)",
                ),
            )

        report_model_cls = ROLE_TO_MODEL[report_role]

        # JSON-MODE validation, not a strict=False override (call-site or
        # otherwise): raw_report_payload is a wire-shaped dict, so every enum
        # field (verdict, role) always arrives as a plain str, never a live
        # Python enum instance. Under the model's own strict=True config,
        # pydantic's Python-OBJECT validation path (model_validate on a dict)
        # requires an ACTUAL enum member for Enum-typed fields and rejects the
        # equal-valued str outright; model_validate_json does NOT hit this --
        # JSON-mode strict validation allows str -> enum construction, because
        # JSON has no native enum type. A blanket `strict=False` override at
        # the Python-object call site was tried first and reverted: pydantic
        # applies it to EVERY ordinary field, not just enums (field-level
        # StrictStr/StrictInt annotations survive it, but that is relying on
        # every current and future field in this model family being
        # explicitly Strict-annotated -- a silent trap for a field that isn't).
        # Routing through JSON preserves strict=True for every field except
        # enums, with no such trap. json.dumps is on a dict[str, object] --
        # if raw_report_payload contains a non-JSON-serializable value that
        # itself is a wire-shape violation, not a crash.
        try:
            serialized_payload = json.dumps(request.raw_report_payload)
        except (TypeError, ValueError) as exc:
            return ModelReportValidationResult(
                correlation_id=request.correlation_id,
                dispatch_role=request.dispatch_role,
                report_role=report_role,
                verdict=EnumReportValidationVerdict.INVALID_SHAPE,
                violations=(
                    f"shape: raw_report_payload is not JSON-serializable: {exc}",
                ),
            )

        try:
            validated_report = report_model_cls.model_validate_json(serialized_payload)
        except ValidationError as exc:
            return ModelReportValidationResult(
                correlation_id=request.correlation_id,
                dispatch_role=request.dispatch_role,
                report_role=report_role,
                verdict=EnumReportValidationVerdict.INVALID_SHAPE,
                violations=_shape_violations_from_error(exc),
            )

        # A probe_result computed for a DIFFERENT correlation_id is untrusted
        # context -- it proves nothing about THIS report's anchor claims, so
        # it is discarded (treated as absent) before the per-field check runs.
        # A discarded probe naturally reproduces ANCHOR_UNCHECKABLE for
        # whichever fields actually needed it (the same code path as
        # probe_result=None); a report with no anchor claims at all is
        # unaffected either way.
        probe_result = request.probe_result
        if (
            probe_result is not None
            and probe_result.correlation_id != request.correlation_id
        ):
            probe_result = None

        missing_context, unresolved = _check_content_anchors(
            validated_report, probe_result
        )

        if missing_context:
            return ModelReportValidationResult(
                correlation_id=request.correlation_id,
                dispatch_role=request.dispatch_role,
                report_role=report_role,
                verdict=EnumReportValidationVerdict.ANCHOR_UNCHECKABLE,
                violations=missing_context + unresolved,
            )

        if unresolved:
            return ModelReportValidationResult(
                correlation_id=request.correlation_id,
                dispatch_role=request.dispatch_role,
                report_role=report_role,
                verdict=EnumReportValidationVerdict.INVALID_CONTENT,
                violations=unresolved,
            )

        return ModelReportValidationResult(
            correlation_id=request.correlation_id,
            dispatch_role=request.dispatch_role,
            report_role=report_role,
            verdict=EnumReportValidationVerdict.VALID,
            violations=(),
        )


__all__ = ["HandlerReportValidation"]
