# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_dashboard_sweep (OMN-13683, WS-5 Wave 9).

Variant A (EFFECT/COMPUTE): drives the real ``NodeDashboardSweep.handle`` over a
matrix of pre-classified page inputs. In ``pages`` mode (no ``base_url``) the
handler performs NO HTTP I/O — the classification + triage runs fully in-memory,
which is exactly the read-only path this wave requires. We never set ``base_url``,
so the live HTTP recon path is never exercised.

Asserts typed result fields: ``status`` (clean | issues_found), ``pages_total``,
the ``by_status`` count map, the triaged ``domains`` (count + ``fix_tier``). The
negative-control cases supply known-bad pages (JS error, network error, empty)
and assert a finding (a problem domain) is produced.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    DashboardSweepRequest,
    EnumFixTier,
    EnumPageStatus,
    ModelPageInput,
    NodeDashboardSweep,
)


def _healthy(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_data=True, has_live_timestamps=True)


def _empty(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_data=False)


def _js_broken(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_js_errors=True)


def _net_broken(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_network_errors=True)


def _mock(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_mock_patterns=True)


def _flag_gated(route: str) -> ModelPageInput:
    return ModelPageInput(route=route, has_feature_flag=True)


# id, pages, dry_run, expected_status, expected_total, expected_by_status (subset),
# expected_domain_count, expected_fix_tiers (set)
_CASES = [
    pytest.param(
        [_healthy("/"), _healthy("/agents")],
        False,
        "clean",
        2,
        {EnumPageStatus.HEALTHY: 2},
        0,
        set(),
        id="all-healthy-clean",
    ),
    pytest.param(
        [_healthy("/"), _js_broken("/agents")],
        False,
        "issues_found",
        2,
        {EnumPageStatus.HEALTHY: 1, EnumPageStatus.BROKEN: 1},
        1,
        {EnumFixTier.CODE_BUG},
        id="one-js-broken-codebug",
    ),
    pytest.param(
        [_healthy("/"), _net_broken("/events")],
        False,
        "issues_found",
        2,
        {EnumPageStatus.BROKEN: 1},
        1,
        {EnumFixTier.CODE_BUG},
        id="network-error-codebug",
    ),
    pytest.param(
        [_healthy("/"), _empty("/metrics")],
        False,
        "issues_found",
        2,
        {EnumPageStatus.EMPTY: 1},
        1,
        {EnumFixTier.DATA_PIPELINE},
        id="empty-page-datapipeline",
    ),
    pytest.param(
        [_mock("/intelligence"), _flag_gated("/delegation")],
        False,
        "clean",
        2,
        {EnumPageStatus.MOCK: 1, EnumPageStatus.FLAG_GATED: 1},
        0,
        set(),
        id="mock-and-flag-not-findings",
    ),
    pytest.param(
        [
            _healthy("/"),
            _js_broken("/agents"),
            _empty("/metrics"),
            _net_broken("/events"),
        ],
        True,
        "issues_found",
        4,
        {
            EnumPageStatus.HEALTHY: 1,
            EnumPageStatus.BROKEN: 2,
            EnumPageStatus.EMPTY: 1,
        },
        3,
        {EnumFixTier.CODE_BUG, EnumFixTier.DATA_PIPELINE},
        id="mixed-multi-finding-dry-run",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "pages",
        "dry_run",
        "exp_status",
        "exp_total",
        "exp_by_status",
        "exp_domains",
        "exp_tiers",
    ),
    _CASES,
)
def test_dashboard_sweep_multiparam(
    pages: list[ModelPageInput],
    dry_run: bool,
    exp_status: str,
    exp_total: int,
    exp_by_status: dict[EnumPageStatus, int],
    exp_domains: int,
    exp_tiers: set[EnumFixTier],
) -> None:
    result = NodeDashboardSweep().handle(
        DashboardSweepRequest(pages=pages, dry_run=dry_run)
    )

    # No HTTP recon ran in pages mode.
    assert result.recon_results == []
    assert result.pages_total == exp_total
    assert len(result.page_statuses) == exp_total
    assert result.status == exp_status
    assert result.dry_run is dry_run

    # by_status count map matches the supplied page mix.
    for status, count in exp_by_status.items():
        assert result.by_status.get(status) == count

    # Triaged problem domains (the findings).
    assert len(result.domains) == exp_domains
    assert {d.fix_tier for d in result.domains} == exp_tiers

    # Every broken/empty page maps to exactly one domain (a finding).
    finding_routes = {r for d in result.domains for r in d.pages}
    broken_or_empty = {
        ps.route
        for ps in result.page_statuses
        if ps.status in (EnumPageStatus.BROKEN, EnumPageStatus.EMPTY)
    }
    assert finding_routes == broken_or_empty


def test_negative_control_broken_page_yields_finding() -> None:
    """A known-bad page (JS error) MUST produce a problem-domain finding."""
    result = NodeDashboardSweep().handle(
        DashboardSweepRequest(pages=[_js_broken("/agents")])
    )
    assert result.status == "issues_found"
    assert len(result.domains) == 1
    assert result.domains[0].fix_tier == EnumFixTier.CODE_BUG
    assert result.domains[0].pages == ["/agents"]
