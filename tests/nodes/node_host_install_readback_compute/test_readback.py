# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error, and failing-control tests for the host install readback compute (OMN-23957).

* golden: equal render and installed text reads match with both digests named;
* golden: different render and installed text reads mismatch with both digests named;
* golden: an absent installed file reads absent with an empty installed digest;
* error: an empty render text is refused with a typed ValueError naming the unit;
* error: an unknown digest algorithm is refused with a typed ValueError naming the algorithm;
* control: a broken handler that compares only the first 8 hex chars wrongly reports match
  for two texts whose digests share a prefix, so the mismatch test is not vacuous.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text
from omnimarket.nodes.node_host_install_readback_compute.handlers import (
    HandlerHostInstallReadback,
)
from omnimarket.nodes.node_host_install_readback_compute.models import (
    ModelHostInstallReadbackRequest,
    ModelHostInstallReadbackResult,
)
from omnimarket.nodes.node_host_install_readback_compute.overlay import (
    ModelHostInstallReadbackOverlay,
)
from tests.nodes.node_host_install_readback_compute.support import (
    HOST,
    RUN_ID,
    UNIT,
    overlay,
    request,
)

pytestmark = pytest.mark.unit


def handler() -> HandlerHostInstallReadback:
    return HandlerHostInstallReadback(overlay())


def test_golden_match_reads_match_with_both_digests() -> None:
    result = handler().handle(
        request(render_text="rendered unit", installed_text="rendered unit")
    )
    assert result.status == "match"
    assert result.render_digest == result.installed_digest
    assert result.render_digest == digest_text("rendered unit", "sha256")
    assert (result.correlation_id, result.host, result.unit) == (RUN_ID, HOST, UNIT)
    assert result.detail == "render and installed digests match"


def test_golden_mismatch_reads_mismatch_with_both_digests() -> None:
    result = handler().handle(
        request(render_text="rendered unit", installed_text="hand-edited unit")
    )
    assert result.status == "mismatch"
    assert result.render_digest == digest_text("rendered unit", "sha256")
    assert result.installed_digest == digest_text("hand-edited unit", "sha256")
    assert result.render_digest != result.installed_digest
    assert result.detail == "installed digest differs from render digest"


def test_golden_absent_reads_absent_with_empty_installed_digest() -> None:
    result = handler().handle(
        request(render_text="rendered unit", installed_present=False, installed_text="")
    )
    assert result.status == "absent"
    assert result.render_digest == digest_text("rendered unit", "sha256")
    assert result.installed_digest == ""
    assert UNIT in result.detail


def test_error_empty_render_text_refuses_with_typed_value_error() -> None:
    with pytest.raises(ValueError, match=UNIT):
        handler().handle(request(render_text=""))


def test_error_unknown_algorithm_refuses_with_typed_value_error() -> None:
    bad_overlay = ModelHostInstallReadbackOverlay(digest_algorithm="not-a-real-algo")
    broken = HandlerHostInstallReadback(bad_overlay)
    with pytest.raises(ValueError, match="not-a-real-algo"):
        broken.handle(request())


def test_control_length_only_compare_wrongly_reports_match() -> None:
    """A broken handler that compares only digest lengths wrongly reports match for a mismatching pair."""

    class BrokenHandler(HandlerHostInstallReadback):
        def handle(
            self, request: ModelHostInstallReadbackRequest
        ) -> ModelHostInstallReadbackResult:
            if not request.render_text:
                raise ValueError(f"empty render text for unit {request.unit!r}")
            render_digest = digest_text(
                request.render_text, self.overlay.digest_algorithm
            )
            if not request.installed_present:
                return ModelHostInstallReadbackResult(
                    correlation_id=request.correlation_id,
                    host=request.host,
                    unit=request.unit,
                    status="absent",
                    render_digest=render_digest,
                    installed_digest="",
                    detail=f"unit {request.unit} is not installed",
                )
            installed_digest = digest_text(
                request.installed_text, self.overlay.digest_algorithm
            )
            if len(render_digest) == len(installed_digest):
                return ModelHostInstallReadbackResult(
                    correlation_id=request.correlation_id,
                    host=request.host,
                    unit=request.unit,
                    status="match",
                    render_digest=render_digest,
                    installed_digest=installed_digest,
                    detail="render and installed digests match",
                )
            return ModelHostInstallReadbackResult(
                correlation_id=request.correlation_id,
                host=request.host,
                unit=request.unit,
                status="mismatch",
                render_digest=render_digest,
                installed_digest=installed_digest,
                detail="installed digest differs from render digest",
            )

    broken = BrokenHandler(overlay())
    req = request(render_text="rendered unit", installed_text="hand-edited unit")
    assert broken.handle(req).status == "match"
    assert handler().handle(req).status == "mismatch"
