# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-19826): quota comes from response headers only, never the rate_limit endpoint.

``ModelGithubQuotaReading`` is parsed from the ``x-ratelimit-*`` headers of a
response the effect already made. A source scan fails when any file under the
node, its fake transport or its fixtures names the ``rate_limit`` endpoint,
which misreports the shared bucket. The scan carries its own positive control
so a scanner that silently matches nothing cannot pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubQuotaFloor,
    ModelGithubQuotaHeadersMissingError,
    ModelGithubQuotaReading,
    ModelPrLandingGithubFailed,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FIXTURE_DIR,
    all_scenarios,
    load_scenario,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NODE_DIR = (
    _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_pr_landing_github_effect"
)
_CONTRACT = _NODE_DIR / "contract.yaml"
_TEST_DIR = Path(__file__).resolve().parent
# OMN-19831: the shared landing transport every landing call now sends through.
_TRANSPORT_DIR = _REPO_ROOT / "src" / "omnimarket" / "github_landing"

# The endpoint in any spelling a caller would use: a REST path segment
# (``/rate_limit``, ``api.github.com/rate_limit``), ``gh api rate_limit``, or the
# GraphQL ``rateLimit`` object. Names such as ``secondary_rate_limit`` are not
# the endpoint and must not match.
_RATE_LIMIT_ENDPOINT = re.compile(
    r"(?:/rate_limit\b|\bapi\s+[\"']?rate_limit\b|\brateLimit\s*\{)"
)


def _files_under_the_node() -> list[Path]:
    files = [
        p
        for p in _NODE_DIR.rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".json"}
    ]
    files += sorted(_TRANSPORT_DIR.glob("*.py"))
    files += sorted(FIXTURE_DIR.glob("*.json"))
    files += [
        p
        for p in _TEST_DIR.glob("*.py")
        if p.name != Path(__file__).name  # this file quotes the pattern on purpose
    ]
    return files


def test_scanner_positive_control() -> None:
    for text in (
        'rest_json("GET", "/rate_limit", token=token)',
        "https://api.github.com/rate_limit",
        "gh api rate_limit",
        "query { rateLimit { remaining } }",
    ):
        assert _RATE_LIMIT_ENDPOINT.search(text), text
    for text in ("secondary_rate_limit", "PRIMARY_RATE_LIMIT", "x-ratelimit-remaining"):
        assert not _RATE_LIMIT_ENDPOINT.search(text), text


def test_no_code_path_under_the_node_reads_the_rate_limit_endpoint() -> None:
    files = _files_under_the_node()
    # Positive control on the file set: the scan must actually see the node.
    assert _CONTRACT in files
    assert any(p.parent == FIXTURE_DIR for p in files)
    hits = [
        f"{p.relative_to(_REPO_ROOT)}:{n}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _RATE_LIMIT_ENDPOINT.search(line)
    ]
    assert hits == [], f"rate_limit endpoint referenced: {hits}"


def test_no_fixture_request_targets_the_rate_limit_endpoint() -> None:
    for scenario in all_scenarios():
        for exchange in scenario.exchanges:
            assert not _RATE_LIMIT_ENDPOINT.search(exchange.request.path)


def test_reading_is_parsed_from_recorded_headers_case_insensitively() -> None:
    response = load_scenario("read_head_checks_304").exchanges[0].response
    # The recording keeps GitHub's Title-Case header names.
    assert "X-Ratelimit-Remaining" in response.headers
    reading = ModelGithubQuotaReading.from_response_headers(
        response.headers, identity="GITHUB_TOKEN"
    )
    assert reading.limit == 5000
    assert reading.remaining == 4874
    assert reading.used == 126
    assert reading.reset == 1790468175
    assert reading.resource == "core"
    assert reading.identity == "GITHUB_TOKEN"


def test_lowercase_headers_parse_too() -> None:
    headers = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "3398",
        "x-ratelimit-used": "1602",
        "x-ratelimit-reset": "1790465451",
        "x-ratelimit-resource": "graphql",
    }
    reading = ModelGithubQuotaReading.from_response_headers(
        headers, identity="GITHUB_TOKEN"
    )
    assert reading.resource == "graphql"


@pytest.mark.parametrize(
    "missing",
    [
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-used",
        "x-ratelimit-reset",
        "x-ratelimit-resource",
    ],
)
def test_a_missing_header_is_refused_not_defaulted(missing: str) -> None:
    headers = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "1",
        "x-ratelimit-used": "4999",
        "x-ratelimit-reset": "1790465451",
        "x-ratelimit-resource": "core",
    }
    del headers[missing]
    with pytest.raises(ModelGithubQuotaHeadersMissingError, match=missing):
        ModelGithubQuotaReading.from_response_headers(headers, identity="GITHUB_TOKEN")


@pytest.mark.parametrize(
    "identity",
    ["ghp_" + "a" * 36, "ghs_" + "b" * 36, "github_pat_" + "c" * 40, "Bearer xyz"],
)
def test_identity_never_carries_a_token(identity: str) -> None:
    with pytest.raises(ValidationError):
        ModelGithubQuotaReading(
            limit=5000, remaining=1, used=1, reset=1, resource="core", identity=identity
        )


def test_contract_declares_a_header_sourced_quota_floor() -> None:
    floor = ModelGithubQuotaFloor.from_contract(_CONTRACT)
    assert floor.source == "response_headers"
    assert set(floor.min_remaining) == {"core", "graphql"}
    assert all(v > 0 for v in floor.min_remaining.values())


def _reading(remaining: int, resource: str = "core") -> ModelGithubQuotaReading:
    return ModelGithubQuotaReading(
        limit=5000,
        remaining=remaining,
        used=5000 - remaining,
        reset=1790468175,
        resource=resource,
        identity="GITHUB_TOKEN",
    )


def test_floor_refuses_below_and_permits_at_or_above() -> None:
    floor = ModelGithubQuotaFloor.from_contract(_CONTRACT)
    at = floor.min_remaining["core"]
    assert floor.refusal(_reading(at)) is None
    assert (
        floor.refusal(_reading(at - 1)) is EnumPrLandingGithubFailureReason.QUOTA_FLOOR
    )


def test_floor_fails_closed_on_an_undeclared_resource() -> None:
    floor = ModelGithubQuotaFloor.from_contract(_CONTRACT)
    assert (
        floor.refusal(_reading(4999, resource="search"))
        is EnumPrLandingGithubFailureReason.QUOTA_FLOOR
    )


def test_a_quota_floor_refusal_is_a_typed_failed_result_carrying_the_reading() -> None:
    reading = _reading(10)
    failed = ModelPrLandingGithubFailed(
        correlation_id="00000000-0000-4000-8000-000000019826",
        operation=EnumPrLandingGithubOperation.RERUN_RUNS,
        mode=EnumPrLandingGithubMode.ENFORCE,
        repository="OmniNode-ai/omnimarket",
        pr_number=2962,
        head_sha="c5dea513fd94a0e940938f13a798c685ab7a88cb",
        reason=EnumPrLandingGithubFailureReason.QUOTA_FLOOR,
        detail="core remaining 10 is under the declared floor",
        http_status=None,
        retry_after_seconds=None,
        quota=reading,
    )
    assert failed.quota == reading
    with pytest.raises(ValidationError):
        ModelPrLandingGithubFailed.model_validate(
            {**failed.model_dump(), "quota": None}
        )
