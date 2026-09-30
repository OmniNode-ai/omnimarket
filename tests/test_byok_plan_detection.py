# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: onboarding-time plan detection for a multi-plan BYOK provider.

A customer registering a z.ai key does not know, and should not need to know,
whether it belongs to the Coding Plan or the general API: the two are separate
products on separate endpoints, and a key presented to the wrong one is
refused. Detection tries the key against each declared plan's endpoint with a
one-token request and records which one answers.

Every transport here is a mock. No network, no key value in any assertion
message, and one test proves the key never appears in the result or the logs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from omnimarket.routing.byok_plan_detection import (
    ModelByokPlanDetection,
    detect_byok_plan,
)

pytestmark = pytest.mark.unit

CODING = "https://api.z.ai/api/coding/paas/v4/chat/completions"
GENERAL = "https://api.z.ai/api/paas/v4/chat/completions"
KEY = "zai-test-key-do-not-print-0123456789"


def _ok() -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "."}}]}
    )


def _err(status: int, code: str | None = None, message: str = "x") -> httpx.Response:
    body: dict[str, Any] = {"error": {"message": message}}
    if code is not None:
        body["error"]["code"] = code
    return httpx.Response(status, json=body)


def _transport(
    routes: dict[str, httpx.Response | Exception], seen: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = routes[str(request.url)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.MockTransport(handler)


async def _detect(
    routes: dict[str, httpx.Response | Exception],
) -> tuple[ModelByokPlanDetection, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    result = await detect_byok_plan("glm", KEY, transport=_transport(routes, seen))
    return result, seen


class TestDetection:
    async def test_a_coding_plan_key_is_detected_when_the_general_surface_is_down(
        self,
    ) -> None:
        # A surface that could not be reached is no evidence the key also
        # belongs there, so the one plan that answered is the plan.
        result, seen = await _detect({CODING: _ok(), GENERAL: _err(500)})
        assert result.outcome == "detected"
        assert result.plan == "coding_plan"
        # Every plan is probed, in a fixed order: the default plan first.
        assert [str(r.url) for r in seen] == [CODING, GENERAL]

    async def test_a_general_api_key_is_detected_after_the_coding_plan_refuses_it(
        self,
    ) -> None:
        result, seen = await _detect({CODING: _err(401, "1001"), GENERAL: _ok()})
        assert result.outcome == "detected"
        assert result.plan == "general_api"
        assert [str(r.url) for r in seen] == [CODING, GENERAL]

    async def test_a_coding_plan_key_on_the_general_surface_is_not_general(
        self,
    ) -> None:
        # OMN-6790: 1113 is the pay-as-you-go surface refusing a Coding-Plan key.
        result, _ = await _detect({CODING: _ok(), GENERAL: _err(429, "1113")})
        assert result.outcome == "detected"
        assert result.plan == "coding_plan"

    async def test_a_key_that_both_surfaces_answer_is_ambiguous_and_never_chosen(
        self,
    ) -> None:
        # The plans meter differently; picking one is a guess about whose money
        # is spent, so detection refuses and the caller asks the customer.
        result, _ = await _detect({CODING: _ok(), GENERAL: _ok()})
        assert result.outcome == "ambiguous"
        assert result.plan is None
        assert [p.verdict for p in result.probes] == ["answered", "answered"]

    async def test_an_exhausted_window_still_proves_the_plan(self) -> None:
        # 1310: the key authenticated on the coding surface and is capped. That
        # is the plan, not a failure to detect one.
        result, _ = await _detect(
            {
                CODING: _err(429, "1310", "Weekly/Monthly Limit Exhausted"),
                GENERAL: _err(429, "1113"),
            }
        )
        assert result.plan == "coding_plan"

    async def test_a_key_both_surfaces_refuse_is_reported_rejected(self) -> None:
        result, _ = await _detect(
            {CODING: _err(401, "1001"), GENERAL: _err(401, "1001")}
        )
        assert result.outcome == "rejected"
        assert result.plan is None

    async def test_a_200_carrying_a_provider_error_body_is_not_an_answer(
        self,
    ) -> None:
        # OMN-18265: a provider can wrap an error in an HTTP 200.
        wrapped = httpx.Response(200, json={"error": {"code": 401, "message": "no"}})
        result, _ = await _detect({CODING: wrapped, GENERAL: _err(401)})
        assert result.plan is None
        assert result.outcome == "rejected"

    async def test_an_unreachable_or_throttled_surface_is_inconclusive_not_a_guess(
        self,
    ) -> None:
        result, _ = await _detect(
            {CODING: httpx.ConnectError("boom"), GENERAL: _err(503)}
        )
        assert result.outcome == "inconclusive"
        assert result.plan is None

    async def test_one_inconclusive_probe_and_one_rejection_stays_inconclusive(
        self,
    ) -> None:
        # The surface that could not be reached might be the right one.
        result, _ = await _detect(
            {CODING: httpx.ReadTimeout("slow"), GENERAL: _err(401)}
        )
        assert result.outcome == "inconclusive"

    async def test_a_capacity_429_is_inconclusive(self) -> None:
        result, _ = await _detect({CODING: _err(429, "1302"), GENERAL: _err(401)})
        assert result.outcome == "inconclusive"


class TestProbeRequest:
    async def test_the_probe_is_one_token_bearer_auth_to_the_declared_endpoint(
        self,
    ) -> None:
        _, seen = await _detect({CODING: _err(401), GENERAL: _err(401)})
        first = seen[0]
        assert first.method == "POST"
        assert first.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(first.content)
        assert body["max_tokens"] == 1
        assert body["model"] == "glm-5.3-flash"
        assert body["stream"] is False
        second = json.loads(seen[1].content)
        assert second["model"] == "glm-4.5-flash"


class TestNoKeyLeak:
    async def test_the_key_appears_in_neither_the_result_nor_the_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        result, _ = await _detect(
            {CODING: httpx.ConnectError(f"boom {KEY}"), GENERAL: _err(401)}
        )
        assert KEY not in result.model_dump_json()
        assert KEY not in caplog.text
        assert KEY not in repr(result)


class TestProvidersWithoutAChoice:
    async def test_a_single_plan_provider_needs_no_network(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a single-plan provider must not be probed")

        result = await detect_byok_plan(
            "gemini", KEY, transport=httpx.MockTransport(refuse)
        )
        assert result.outcome == "single_plan"
        assert result.plan == "ai_studio"
        assert result.probes == ()

    async def test_an_unoffered_provider_has_no_plan(self) -> None:
        result = await detect_byok_plan("vertex", KEY)
        assert result.outcome == "not_offered"
        assert result.plan is None
