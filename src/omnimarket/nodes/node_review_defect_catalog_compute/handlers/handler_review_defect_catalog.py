# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate and render the defect catalog within the contract's token budget.

Resolution is synchronous and deterministic. The only I/O reads packaged
resources; catalog text and deployed enforcement evidence may be supplied in
the request. Topics and rendering configuration belong to the node contract.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_review_defect_catalog import (
    EnumReviewDefectCatalogOutcome,
    EnumReviewDefectCatalogRefusal,
    EnumReviewDefectFamily,
    EnumReviewDefectSeverity,
    EnumReviewDefectStatus,
)
from omnimarket.models.review_defect_catalog import (
    ModelReviewDefectCatalog,
    ModelReviewDefectCatalogRequest,
    ModelReviewDefectCatalogResult,
    ModelReviewDefectCode,
)
from omnimarket.nodes.node_review_defect_catalog_compute.models import (
    ModelReviewDefectCatalogConfig,
)

_NODE_PACKAGE = "omnimarket.nodes.node_review_defect_catalog_compute"
_FAMILY_LETTERS = {
    EnumReviewDefectFamily.ARCHITECTURE: "A",
    EnumReviewDefectFamily.SECURITY: "S",
    EnumReviewDefectFamily.PERFORMANCE: "P",
    EnumReviewDefectFamily.CORRECTNESS: "C",
    EnumReviewDefectFamily.COMPLETENESS: "K",
    EnumReviewDefectFamily.FEASIBILITY: "F",
    EnumReviewDefectFamily.TESTING: "T",
    EnumReviewDefectFamily.STYLE: "Y",
}
_SEVERITY_MARKERS = {
    EnumReviewDefectSeverity.CRITICAL: "C",
    EnumReviewDefectSeverity.MAJOR: "M",
    EnumReviewDefectSeverity.MINOR: "m",
    EnumReviewDefectSeverity.NIT: "n",
}


def load_catalog_config(
    contract_text: str | None = None,
) -> ModelReviewDefectCatalogConfig:
    """Load the ``catalog`` block from the packaged contract or supplied text."""
    text = (
        contract_text
        if contract_text is not None
        else files(_NODE_PACKAGE).joinpath("contract.yaml").read_text(encoding="utf-8")
    )
    contract = yaml.safe_load(text)
    return ModelReviewDefectCatalogConfig.model_validate(contract["catalog"])


def load_packaged_catalog_text() -> str:
    """Read the packaged catalog at the resource path declared by the contract."""
    config = load_catalog_config()
    return (
        files("omnimarket")
        .joinpath(config.packaged_catalog)
        .read_text(encoding="utf-8")
    )


def _refused(
    reason: EnumReviewDefectCatalogRefusal,
    detail: str,
    *,
    offending_entry: str | None = None,
    catalog_version: str | None = None,
) -> ModelReviewDefectCatalogResult:
    """Return a refusal with no usable section or code collections."""
    return ModelReviewDefectCatalogResult(
        outcome=EnumReviewDefectCatalogOutcome.REFUSED,
        catalog_version=catalog_version,
        content_hash=None,
        rendered_section=None,
        rendered_tokens=None,
        active_codes=(),
        code_families={},
        suppressed_codes=(),
        refusal_reason=reason,
        offending_entry=offending_entry,
        detail=detail,
    )


def _render_section(
    catalog: ModelReviewDefectCatalog, shown: tuple[ModelReviewDefectCode, ...]
) -> str:
    """Render visible entries in catalog order with compact severity markers."""
    lines = [
        f"Defect catalog v{catalog.catalog_version}. Name one code per finding; "
        "R:<id> cites a supplied standing rule; X when no code fits, with a note.",
        "code|family|severity|title: detect",
    ]
    lines.extend(
        f"{entry.code}|{entry.family.value}|"
        f"{_SEVERITY_MARKERS[entry.default_severity]}|{entry.title}: {entry.detect}"
        for entry in shown
    )
    if catalog.do_not_report:
        lines.append("Do not report:")
        lines.extend(f"- {item}" for item in catalog.do_not_report)
    return "\n".join(lines)


class HandlerReviewDefectCatalog:
    """Resolve a valid catalog into a repository's bounded review section."""

    def __init__(self, *, config: ModelReviewDefectCatalogConfig | None = None) -> None:
        self._config = config if config is not None else load_catalog_config()

    def handle(
        self, request: ModelReviewDefectCatalogRequest
    ) -> ModelReviewDefectCatalogResult:
        """Validate, suppress enforced codes, render and hash the catalog."""
        text = (
            request.catalog_text
            if request.catalog_text is not None
            else files("omnimarket")
            .joinpath(self._config.packaged_catalog)
            .read_text(encoding="utf-8")
        )
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return _refused(
                EnumReviewDefectCatalogRefusal.INVALID_CATALOG,
                f"Catalog YAML is invalid: {exc}",
            )
        if not isinstance(data, dict):
            return _refused(
                EnumReviewDefectCatalogRefusal.INVALID_CATALOG,
                "Catalog must be a mapping.",
            )
        try:
            catalog = ModelReviewDefectCatalog.model_validate(data)
        except ValidationError as exc:
            error = exc.errors()[0]
            return _refused(
                EnumReviewDefectCatalogRefusal.INVALID_CATALOG,
                error["msg"],
                offending_entry=".".join(str(part) for part in error["loc"]),
            )

        version = catalog.catalog_version
        if (
            request.requested_version is not None
            and request.requested_version != version
        ):
            return _refused(
                EnumReviewDefectCatalogRefusal.VERSION_MISMATCH,
                f"Requested version {request.requested_version} differs from {version}.",
                offending_entry=request.requested_version,
                catalog_version=version,
            )

        seen_codes: set[str] = set()
        seen_slugs: set[str] = set()
        for entry in catalog.codes:
            if entry.code in seen_codes:
                return _refused(
                    EnumReviewDefectCatalogRefusal.DUPLICATE_CODE,
                    f"Code {entry.code} appears more than once, including retired entries.",
                    offending_entry=entry.code,
                    catalog_version=version,
                )
            if entry.slug in seen_slugs:
                return _refused(
                    EnumReviewDefectCatalogRefusal.DUPLICATE_CODE,
                    f"Slug {entry.slug} appears more than once.",
                    offending_entry=entry.slug,
                    catalog_version=version,
                )
            seen_codes.add(entry.code)
            seen_slugs.add(entry.slug)

        for entry in catalog.codes:
            if entry.code[0] != _FAMILY_LETTERS[entry.family]:
                return _refused(
                    EnumReviewDefectCatalogRefusal.CODE_FAMILY_MISMATCH,
                    f"Code {entry.code} does not match family {entry.family.value}.",
                    offending_entry=entry.code,
                    catalog_version=version,
                )

        for entry in catalog.codes:
            if any(
                not validator.validator_version
                for validator in entry.enforced_by.values()
            ):
                return _refused(
                    EnumReviewDefectCatalogRefusal.ENFORCED_BY_WITHOUT_VERSION,
                    f"Code {entry.code} has an enforcement record without a validator version.",
                    offending_entry=entry.code,
                    catalog_version=version,
                )

        for code in request.enforced_codes:
            if code not in seen_codes:
                return _refused(
                    EnumReviewDefectCatalogRefusal.UNKNOWN_CODE,
                    f"Enforced code {code} is absent from the catalog.",
                    offending_entry=code,
                    catalog_version=version,
                )

        enforced = set(request.enforced_codes)
        active = tuple(
            entry
            for entry in catalog.codes
            if entry.status is EnumReviewDefectStatus.ACTIVE
        )
        suppressed = tuple(
            entry.code
            for entry in active
            if entry.code in enforced and request.repository in entry.enforced_by
        )
        suppressed_set = set(suppressed)
        shown = tuple(entry for entry in active if entry.code not in suppressed_set)
        section = _render_section(catalog, shown)
        chars_per_token = self._config.chars_per_token
        rendered_tokens = (len(section) + chars_per_token - 1) // chars_per_token
        budget = (
            request.token_budget
            if request.token_budget is not None
            else self._config.default_token_budget
        )
        if rendered_tokens > budget:
            return _refused(
                EnumReviewDefectCatalogRefusal.SECTION_OVER_BUDGET,
                f"Rendered section needs {rendered_tokens} tokens; budget is {budget}.",
                catalog_version=version,
            )

        canonical = json.dumps(
            catalog.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return ModelReviewDefectCatalogResult(
            outcome=EnumReviewDefectCatalogOutcome.RESOLVED,
            catalog_version=version,
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            rendered_section=section,
            rendered_tokens=rendered_tokens,
            active_codes=(*(entry.code for entry in shown), "X"),
            code_families={entry.code: entry.family.value for entry in catalog.codes},
            suppressed_codes=suppressed,
            refusal_reason=None,
            offending_entry=None,
            detail=f"Resolved catalog v{version} with {len(shown)} visible defect codes.",
        )


__all__ = [
    "HandlerReviewDefectCatalog",
    "load_catalog_config",
    "load_packaged_catalog_text",
]
