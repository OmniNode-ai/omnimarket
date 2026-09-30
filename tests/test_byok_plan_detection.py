# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: onboarding-time plan detection for a multi-plan BYOK provider.

A customer registering a z.ai key does not know, and should not need to know,
whether it belongs to the Coding Plan or the general API: the two are separate
products on separate endpoints, and a key presented to the wrong one is
refused. Detection tries the key against the general API first with a
one-token request. A key that answers there is a general key, and the Coding
Plan endpoint is never contacted. Only a key the general API refused is tried
on the Coding Plan surface, and one that answers there alone is refused with a
typed ``BYOK_CODING_PLAN_NOT_PERMITTED``: z.ai's subscription terms bar Coding
Plan quota from third-party systems (knowledge-base-internal
``reference/zai-glm-coding-plan-terms.md``), so no customer route may use it.

Every transport here is a mock. No network, no key value in any assertion
message, and one test proves the key never appears in the result or the logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    ModelTransportResponse,
)
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


def _post(
    routes: dict[str, httpx.Response | Exception],
    seen: list[str],
    calls: list[dict[str, Any]] | None = None,
) -> Callable[..., ModelTransportResponse]:
    """A stand-in for the contract transport, answering from ``routes``.

    It behaves like the real one: a 2xx returns the body, any other status
    raises ``httpx.HTTPStatusError`` carrying the provider's own response.
    """

    def post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> ModelTransportResponse:
        seen.append(endpoint_url)
        if calls is not None:
            calls.append(
                {"url": endpoint_url, "payload": payload, "headers": extra_headers}
            )
        assert payload["max_tokens"] == 1
        outcome = routes[endpoint_url]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.status_code >= 400:
            raise httpx.HTTPStatusError(
                "refused",
                request=httpx.Request("POST", endpoint_url),
                response=outcome,
            )
        return ModelTransportResponse(
            status_code=outcome.status_code, json_body=outcome.json(), latency_ms=1
        )

    return post


async def _detect(
    routes: dict[str, httpx.Response | Exception],
) -> tuple[ModelByokPlanDetection, list[str]]:
    seen: list[str] = []
    result = await detect_byok_plan("glm", KEY, post=_post(routes, seen))
    return result, seen


class TestDetection:
    async def test_a_general_api_key_is_detected_and_routes_general(self) -> None:
        result, seen = await _detect({GENERAL: _ok(), CODING: _err(401, "1001")})
        assert result.outcome == "detected"
        assert result.plan == "general_api"
        assert result.refused_plan is None
        # The general API answered, so the Coding Plan surface is never touched.
        assert seen == [GENERAL]

    async def test_a_coding_plan_only_key_is_refused_with_the_typed_code(
        self,
    ) -> None:
        # OMN-6790: 1113 is the pay-as-you-go surface refusing a Coding-Plan key.
        result, seen = await _detect({GENERAL: _err(429, "1113"), CODING: _ok()})
        assert result.outcome == "not_permitted"
        assert result.plan is None
        assert result.refused_plan == "coding_plan"
        assert result.refusal_code == "BYOK_CODING_PLAN_NOT_PERMITTED"
        # The general API is asked first; the Coding Plan only after it refused.
        assert seen == [GENERAL, CODING]

    async def test_a_key_both_surfaces_answer_is_a_general_key_never_refused(
        self,
    ) -> None:
        # The platform's own z.ai key answers on both surfaces. A key the
        # general API accepts is usable there whatever else it can do.
        result, seen = await _detect({GENERAL: _ok(), CODING: _ok()})
        assert result.outcome == "detected"
        assert result.plan == "general_api"
        assert result.refusal_code is None
        assert seen == [GENERAL], "the Coding Plan endpoint must not be contacted"

    async def test_an_exhausted_coding_window_still_proves_the_key_is_coding_only(
        self,
    ) -> None:
        # 1310: the key authenticated on the coding surface and is capped. It is
        # still a Coding Plan key, and still not permitted.
        result, _ = await _detect(
            {
                GENERAL: _err(429, "1113"),
                CODING: _err(429, "1310", "Weekly/Monthly Limit Exhausted"),
            }
        )
        assert result.outcome == "not_permitted"
        assert result.refused_plan == "coding_plan"

    async def test_an_exhausted_general_window_is_still_a_general_key(self) -> None:
        result, seen = await _detect(
            {GENERAL: _err(429, "1308", "window spent"), CODING: _ok()}
        )
        assert result.outcome == "detected"
        assert result.plan == "general_api"
        assert seen == [GENERAL]

    async def test_a_general_surface_that_could_not_answer_never_refuses_the_key(
        self,
    ) -> None:
        # A throttled or unreachable general API is no evidence the key is not a
        # general key, so a Coding Plan answer alone must not refuse it.
        result, _ = await _detect({GENERAL: _err(500), CODING: _ok()})
        assert result.outcome == "inconclusive"
        assert result.plan is None
        assert result.refused_plan is None
        assert result.refusal_code is None

    async def test_a_key_both_surfaces_refuse_is_reported_rejected(self) -> None:
        result, _ = await _detect(
            {GENERAL: _err(401, "1001"), CODING: _err(401, "1001")}
        )
        assert result.outcome == "rejected"
        assert result.plan is None
        assert result.refusal_code is None

    async def test_a_200_carrying_a_provider_error_body_is_not_an_answer(
        self,
    ) -> None:
        # OMN-18265: a provider can wrap an error in an HTTP 200.
        wrapped = httpx.Response(200, json={"error": {"code": 401, "message": "no"}})
        result, _ = await _detect({GENERAL: wrapped, CODING: _err(401)})
        assert result.plan is None
        assert result.outcome == "rejected"

    async def test_an_unreachable_or_throttled_surface_is_inconclusive_not_a_guess(
        self,
    ) -> None:
        result, _ = await _detect(
            {GENERAL: httpx.ConnectError("boom"), CODING: _err(503)}
        )
        assert result.outcome == "inconclusive"
        assert result.plan is None

    async def test_one_inconclusive_probe_and_one_rejection_stays_inconclusive(
        self,
    ) -> None:
        # The surface that could not be reached might be the right one.
        result, _ = await _detect(
            {GENERAL: httpx.ReadTimeout("slow"), CODING: _err(401)}
        )
        assert result.outcome == "inconclusive"

    async def test_a_capacity_429_is_inconclusive(self) -> None:
        result, _ = await _detect({GENERAL: _err(429, "1302"), CODING: _err(401)})
        assert result.outcome == "inconclusive"


class TestProbeRequest:
    async def test_the_probe_is_one_token_bearer_auth_to_the_general_endpoint_first(
        self,
    ) -> None:
        calls: list[dict[str, Any]] = []
        await detect_byok_plan(
            "glm",
            KEY,
            post=_post({GENERAL: _err(401), CODING: _err(401)}, [], calls),
        )
        first = calls[0]
        assert first["url"] == GENERAL
        assert first["headers"] == {"Authorization": f"Bearer {KEY}"}
        body = first["payload"]
        assert body["max_tokens"] == 1
        assert body["model"] == "glm-4.5-flash"
        assert body["stream"] is False
        assert calls[1]["url"] == CODING
        assert calls[1]["payload"]["model"] == "glm-5.3-flash"


class TestNoKeyLeak:
    async def test_the_key_appears_in_neither_the_result_nor_the_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        result, _ = await _detect(
            {GENERAL: httpx.ConnectError(f"boom {KEY}"), CODING: _err(401)}
        )
        assert KEY not in result.model_dump_json()
        assert KEY not in caplog.text
        assert KEY not in repr(result)


class TestProvidersWithoutAChoice:
    async def test_a_single_plan_provider_needs_no_network(self) -> None:
        result = await detect_byok_plan("gemini", KEY, post=_post({}, []))
        assert result.outcome == "single_plan"
        assert result.plan == "ai_studio"
        assert result.probes == ()

    async def test_an_unoffered_provider_has_no_plan(self) -> None:
        result = await detect_byok_plan("vertex", KEY)
        assert result.outcome == "not_offered"
        assert result.plan is None
