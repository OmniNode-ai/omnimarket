# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15777 hardening: ``_iter_open_prs`` pagination had ZERO discriminating
test coverage.

The generator's own docstring claims it "paginat[es] past the first 100" —
correct as shipped — but nothing in the existing suite actually drives a
second page: every fixture in ``test_reuse_open_observation_pr_omn_15777.py``
returns at most a handful of open PRs, so a single-page ``rest_json_array``
call always satisfies every assertion. A mutation that collapsed the loop to
``range(1, 2)`` (fetch page 1 only, ignore ``_MAX_OPEN_PR_PAGES``) would leave
every test in this package green.

This test seeds a full 100-item first page (no matching PR) plus a
short second page that DOES carry the matching PR, and asserts the matching
PR is actually yielded — which requires page 2 to be fetched. It is
hermetic: only ``rest_json_array`` is stubbed, no git, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_occ_observation_effect.handlers."
    "handler_occ_observation_effect"
)


def _pr(number: int, ref: str) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/OmniNode-ai/onex_change_control/pull/{number}",
        "head": {
            "ref": ref,
            "repo": {"full_name": "OmniNode-ai/onex_change_control"},
        },
    }


class _PagedPrListing:
    """Fake ``rest_json_array`` serving PRs from a fixed page map.

    Records every ``page=`` value it was asked for, so a test can also assert
    the generator actually requested more than one page (belt-and-suspenders
    against a mutant that silently drops pagination while still — by luck —
    yielding the right items from a differently-shaped fake).
    """

    def __init__(self, pages: dict[int, list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.requested_pages: list[int] = []

    def __call__(self, _method: str, path: str, **_kwargs: Any) -> list[dict[str, Any]]:
        # Split on "&" and match the "page=" query param EXACTLY -- a naive
        # substring search (`"page=" in path`) also matches inside
        # "per_page=100", which corrupts the parsed page number.
        query = path.split("?", 1)[-1]
        page = 1
        for param in query.split("&"):
            if param.startswith("page="):
                page = int(param[len("page=") :])
                break
        self.requested_pages.append(page)
        return list(self._pages.get(page, []))


@pytest.mark.unit
def test_iter_open_prs_paginates_past_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR that only appears on page 2 must still be yielded.

    Page 1 is a FULL 100-item page (the exact condition that keeps the
    generator looping) containing nothing that matches; page 2 is a short
    (<100) page carrying the one PR under test. If pagination regressed to
    ``range(1, 2)`` (page 1 only), this PR would never be observed.
    """
    page_one = [_pr(1000 + i, f"unrelated/branch-{i}") for i in range(100)]
    page_two = [_pr(9999, "auto/occ-observation-on-page-two")]
    fake = _PagedPrListing({1: page_one, 2: page_two})
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json_array", fake)

    handler = HandlerOccObservationEffect()
    found = list(
        handler._iter_open_prs("OmniNode-ai", "onex_change_control", "tok", "asc")
    )

    numbers = {pr["number"] for pr in found}
    assert 9999 in numbers, (
        "the page-2-only PR must be yielded -- pagination must have run past "
        "the first page"
    )
    assert len(found) == 101, "both pages' items must be yielded, not just page 1"
    assert 2 in fake.requested_pages, (
        "the generator must actually have requested page 2, not merely "
        "happened to find the right count some other way"
    )


@pytest.mark.unit
def test_iter_open_prs_stops_at_first_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page shorter than 100 ends pagination — no page 3 is ever requested.

    Sibling of the growth-direction test above: without this, a mutant that
    always fetches every page up to ``_MAX_OPEN_PR_PAGES`` (ignoring the
    short-page stop condition) would also pass the "does it paginate" test
    while silently issuing far more requests than necessary in production.
    """
    page_one = [_pr(1000 + i, f"unrelated/branch-{i}") for i in range(100)]
    page_two = [_pr(9999, "auto/occ-observation-on-page-two")]
    fake = _PagedPrListing({1: page_one, 2: page_two})
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json_array", fake)

    handler = HandlerOccObservationEffect()
    list(handler._iter_open_prs("OmniNode-ai", "onex_change_control", "tok", "asc"))

    assert fake.requested_pages == [1, 2], (
        "pagination must stop as soon as a short (<100) page is returned"
    )


@pytest.mark.unit
def test_iter_open_prs_honors_max_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misbehaving API that never returns a short page must still terminate
    at ``_MAX_OPEN_PR_PAGES`` rather than looping forever."""

    def _always_full_page(
        _method: str, _path: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        return [_pr(1, "unrelated/branch")] * 100

    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json_array", _always_full_page)

    handler = HandlerOccObservationEffect()
    found = list(
        handler._iter_open_prs("OmniNode-ai", "onex_change_control", "tok", "asc")
    )

    assert len(found) == handler._MAX_OPEN_PR_PAGES * 100
