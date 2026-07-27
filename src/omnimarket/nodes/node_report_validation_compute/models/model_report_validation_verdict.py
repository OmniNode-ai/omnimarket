# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed verdict vocabulary for the report-validation COMPUTE node (OMN-15163)."""

from __future__ import annotations

from enum import StrEnum


class EnumReportValidationVerdict(StrEnum):
    """Outcome of validating one raw dispatch-report payload.

    ``VALID`` is the only passing verdict. The other three are all failure
    verdicts, distinguished by WHICH of the two enforcement checks
    (``worker result -> validate (shape AND content anchors)``, per the
    ticket brief) rejected the report, so a caller can report a SPECIFIC
    reason rather than a bare reject:

    - ``INVALID_SHAPE`` — the raw payload dict does not construct into the
      role-resolved ``omnibase_core.models.dispatch.report`` model (missing/
      extra/mis-typed fields, or one of that model's own field_validators
      rejected the content, e.g. a placeholder/bare-acknowledgement summary —
      those validators raise pydantic ``ValidationError`` the same as a type
      mismatch, so they are a shape failure by construction, not a content-
      anchor failure). Also returned when ``dispatch_role`` has no mapped
      report role at all (declared out-of-scope; see ``ROLE_MAPPING_TABLE``).
    - ``INVALID_CONTENT`` — the payload constructs cleanly, but a content-
      anchor field it claims (a ``*_sha`` or ``*_paths`` field, per the core
      anchor-validator's field-name-suffix convention) was checked by the
      caller-supplied probe result and did NOT resolve.
    - ``ANCHOR_UNCHECKABLE`` — the payload constructs cleanly and claims a
      content-anchor field, but the caller supplied no matching probe result
      for it (missing anchor-checking context). This is a FAILURE verdict,
      never treated as passing or silently skipped
      (``feedback_optional_input_means_the_check_does_not_exist``): an
      unchecked anchor claim is exactly as untrustworthy as a positively
      wrong one.
    """

    VALID = "valid"
    INVALID_SHAPE = "invalid_shape"
    INVALID_CONTENT = "invalid_content"
    ANCHOR_UNCHECKABLE = "anchor_uncheckable"


__all__ = ["EnumReportValidationVerdict"]
