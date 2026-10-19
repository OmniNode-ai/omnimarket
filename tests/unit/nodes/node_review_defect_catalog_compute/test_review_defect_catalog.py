# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer's defect-code catalog, validated and rendered (OMN-20910).

Every refusal has a positive control: the same catalog with the one offending
entry fixed resolves.
"""

from __future__ import annotations

import copy
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_review_defect_catalog import (
    EnumReviewDefectCatalogOutcome,
    EnumReviewDefectCatalogRefusal,
)
from omnimarket.models.review_defect_catalog import (
    MAX_REVIEW_DEFECT_TOKEN_BUDGET,
    ModelReviewDefectCatalogRequest,
    ModelReviewDefectCatalogResult,
)
from omnimarket.nodes.node_review_defect_catalog_compute import (
    HandlerReviewDefectCatalog,
    NodeReviewDefectCatalogCompute,
)
from omnimarket.nodes.node_review_defect_catalog_compute.handlers.handler_review_defect_catalog import (
    load_packaged_catalog_text,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE = ROOT / "src/omnimarket/nodes/node_review_defect_catalog_compute"
REPO = "omnimarket"


def _packaged() -> dict[str, Any]:
    data = yaml.safe_load(load_packaged_catalog_text())
    assert isinstance(data, dict)
    return data


def _resolve(
    data: dict[str, Any] | None = None, **request: Any
) -> ModelReviewDefectCatalogResult:
    text = None if data is None else yaml.safe_dump(data, sort_keys=False)
    return HandlerReviewDefectCatalog().handle(
        ModelReviewDefectCatalogRequest(
            catalog_text=text, repository=request.pop("repository", REPO), **request
        )
    )


def _code(data: dict[str, Any], code: str) -> dict[str, Any]:
    return next(c for c in data["codes"] if c["code"] == code)


def _refused(
    result: ModelReviewDefectCatalogResult, reason: EnumReviewDefectCatalogRefusal
) -> None:
    assert result.outcome is EnumReviewDefectCatalogOutcome.REFUSED, result.detail
    assert result.refusal_reason is reason, result.detail
    assert result.rendered_section is None
    assert result.active_codes == ()


def _resolved(result: ModelReviewDefectCatalogResult) -> None:
    assert result.outcome is EnumReviewDefectCatalogOutcome.RESOLVED, result.detail
    assert result.refusal_reason is None


# --- the packaged catalog -------------------------------------------------


def test_packaged_catalog_resolves_within_the_budget() -> None:
    result = _resolve()
    _resolved(result)
    data = _packaged()
    active = [c["code"] for c in data["codes"] if c["status"] == "active"]
    assert 25 <= len(active) <= 40
    assert result.catalog_version == data["catalog_version"]
    assert result.active_codes == (*active, "X")
    assert result.rendered_tokens is not None
    assert 0 < result.rendered_tokens <= 5000
    assert result.content_hash is not None
    assert re.fullmatch(r"[0-9a-f]{64}", result.content_hash)
    assert result.code_families["T01"] == "testing"
    for code in active:
        assert re.search(rf"^{code}\|", result.rendered_section or "", re.M), code
    for line in data["do_not_report"]:
        assert line in (result.rendered_section or "")


def test_resolution_is_deterministic() -> None:
    first, second = _resolve(), _resolve()
    assert first.content_hash == second.content_hash
    assert first.rendered_section == second.rendered_section


def test_hash_moves_when_an_entry_changes() -> None:
    data = _packaged()
    _code(data, "T01")["fix_hint"] = "Assert what the code returns."
    assert _resolve(data).content_hash != _resolve().content_hash


def test_requested_version_must_match() -> None:
    _refused(
        _resolve(requested_version="9.9.9"),
        EnumReviewDefectCatalogRefusal.VERSION_MISMATCH,
    )
    _resolved(_resolve(requested_version=_packaged()["catalog_version"]))


# --- refusals, each with its positive control ----------------------------


def test_duplicate_code_is_refused() -> None:
    data = _packaged()
    data["codes"].append(copy.deepcopy(_code(data, "T01")))
    data["codes"][-1]["slug"] = "another-slug"
    result = _resolve(data)
    _refused(result, EnumReviewDefectCatalogRefusal.DUPLICATE_CODE)
    assert result.offending_entry == "T01"
    data["codes"].pop()
    _resolved(_resolve(data))


def test_retired_code_id_is_never_reused() -> None:
    data = _packaged()
    retired = copy.deepcopy(_code(data, "C02"))
    retired.update(status="retired", slug="old-swallowed-error", retired_in="0.1.0")
    data["codes"].append(retired)
    result = _resolve(data)
    _refused(result, EnumReviewDefectCatalogRefusal.DUPLICATE_CODE)
    assert result.offending_entry == "C02"
    # Control: the same retired entry under a fresh id is accepted and not shown.
    retired["code"] = "C99"
    resolved = _resolve(data)
    _resolved(resolved)
    assert "C99" not in resolved.active_codes


def test_duplicate_slug_is_refused() -> None:
    data = _packaged()
    original = _code(data, "T02")["slug"]
    _code(data, "T02")["slug"] = _code(data, "T01")["slug"]
    _refused(_resolve(data), EnumReviewDefectCatalogRefusal.DUPLICATE_CODE)
    _code(data, "T02")["slug"] = original
    _resolved(_resolve(data))


def test_family_letter_must_match_family() -> None:
    data = _packaged()
    _code(data, "T03")["family"] = "security"
    result = _resolve(data)
    _refused(result, EnumReviewDefectCatalogRefusal.CODE_FAMILY_MISMATCH)
    assert result.offending_entry == "T03"
    _code(data, "T03")["family"] = "testing"
    _resolved(_resolve(data))


@pytest.mark.parametrize("bad", ["X01", "R01", "T1", "t01", "T001"])
def test_reserved_or_malformed_code_is_invalid(bad: str) -> None:
    data = _packaged()
    _code(data, "Y02")["code"] = bad
    _refused(_resolve(data), EnumReviewDefectCatalogRefusal.INVALID_CATALOG)


def test_family_outside_the_eight_is_invalid() -> None:
    data = _packaged()
    _code(data, "Y02")["family"] = "vibes"
    result = _resolve(data)
    _refused(result, EnumReviewDefectCatalogRefusal.INVALID_CATALOG)
    assert "codes" in (result.offending_entry or "")


def test_word_limits_are_enforced() -> None:
    data = _packaged()
    _code(data, "Y02")["title"] = " ".join(["word"] * 13)
    _refused(_resolve(data), EnumReviewDefectCatalogRefusal.INVALID_CATALOG)
    data = _packaged()
    _code(data, "Y02")["detect"] = " ".join(["word"] * 41)
    _refused(_resolve(data), EnumReviewDefectCatalogRefusal.INVALID_CATALOG)


def test_enforced_by_without_validator_version_is_refused() -> None:
    data = _packaged()
    _code(data, "A03")["enforced_by"] = {REPO: {"validator_id": "check-topics"}}
    result = _resolve(data)
    _refused(result, EnumReviewDefectCatalogRefusal.ENFORCED_BY_WITHOUT_VERSION)
    assert result.offending_entry == "A03"
    _code(data, "A03")["enforced_by"][REPO]["validator_version"] = "1.0.0"
    _resolved(_resolve(data))


def test_rendered_section_over_budget_is_refused() -> None:
    _refused(
        _resolve(token_budget=100),
        EnumReviewDefectCatalogRefusal.SECTION_OVER_BUDGET,
    )
    _resolved(_resolve(token_budget=5000))


def test_token_budget_above_the_ceiling_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelReviewDefectCatalogRequest(
            repository=REPO, token_budget=MAX_REVIEW_DEFECT_TOKEN_BUDGET + 1
        )
    ModelReviewDefectCatalogRequest(
        repository=REPO, token_budget=MAX_REVIEW_DEFECT_TOKEN_BUDGET
    )


def test_unknown_enforced_code_is_refused() -> None:
    _refused(
        _resolve(enforced_codes=("Q99",)),
        EnumReviewDefectCatalogRefusal.UNKNOWN_CODE,
    )
    _resolved(_resolve(enforced_codes=("T01",)))


def test_malformed_yaml_is_invalid() -> None:
    result = HandlerReviewDefectCatalog().handle(
        ModelReviewDefectCatalogRequest(catalog_text="codes: [", repository=REPO)
    )
    _refused(result, EnumReviewDefectCatalogRefusal.INVALID_CATALOG)


# --- what the reviewer is shown ------------------------------------------


def test_retired_codes_are_not_rendered_or_allowed() -> None:
    data = _packaged()
    _code(data, "Y02").update(status="retired", retired_in="0.1.0")
    result = _resolve(data)
    _resolved(result)
    assert "Y02" not in result.active_codes
    assert not re.search(r"^Y02\|", result.rendered_section or "", re.M)
    assert result.code_families["Y02"] == "style"


def test_code_leaves_only_the_repository_whose_validator_enforces_it() -> None:
    data = _packaged()
    _code(data, "A03")["enforced_by"] = {
        REPO: {"validator_id": "check-topics", "validator_version": "1.0.0"}
    }
    here = _resolve(data, enforced_codes=("A03",))
    _resolved(here)
    assert "A03" not in here.active_codes
    assert here.suppressed_codes == ("A03",)
    assert not re.search(r"^A03\|", here.rendered_section or "", re.M)

    elsewhere = _resolve(data, repository="omniclaude", enforced_codes=("A03",))
    _resolved(elsewhere)
    assert "A03" in elsewhere.active_codes
    assert elsewhere.suppressed_codes == ()


def test_run_evidence_without_a_catalog_record_keeps_the_code() -> None:
    result = _resolve(enforced_codes=("A03",))
    _resolved(result)
    assert "A03" in result.active_codes
    assert result.suppressed_codes == ()


# --- node shape -----------------------------------------------------------


def test_handler_has_the_canonical_signature() -> None:
    hints = inspect.get_annotations(HandlerReviewDefectCatalog.handle, eval_str=True)
    assert hints["request"] is ModelReviewDefectCatalogRequest
    assert hints["return"] is ModelReviewDefectCatalogResult
    assert issubclass(NodeReviewDefectCatalogCompute, HandlerReviewDefectCatalog)


def test_contract_declares_the_topics_and_the_budget() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.review-defect-catalog-resolve-requested.v1"
    )
    assert (
        dispatch["terminal_events"]["success"]
        == "onex.evt.omnimarket.review-defect-catalog-resolved.v1"
    )
    assert (
        dispatch["terminal_events"]["failure"]
        == "onex.evt.omnimarket.review-defect-catalog-resolve-failed.v1"
    )
    assert contract["catalog"]["default_token_budget"] == 5000


def test_entry_point_is_registered() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'node_review_defect_catalog_compute = "omnimarket.nodes.'
        'node_review_defect_catalog_compute"' in pyproject
    )
