# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EvaluatorDharma against recorded Dharma responses (OMN-20088)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_dharma import (
    INSTRUCTIONS,
    EvaluatorDharma,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_dharma import (
    TASK_MAX_CHARS,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorError,
    EvaluatorRateLimitedError,
    EvaluatorRejectedError,
    EvaluatorTransportError,
    ModelEvaluatorSubmission,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "dharma"
TOKEN = "test-token-not-real-0123456789"
ORG = "org_fixture"
BASE = "https://dharma.example.test"
CAMPAIGNS = f"/api/orgs/{ORG}/managed-evals/campaigns"
# The key whose derived campaign name the recorded BLOCK campaign carries.
RECORDED_KEY = "onex-eval-full-763c3857-4cb0-4c00-9967-c8d3eb50eb1e"
RECORDED_ID = "e0abc39a-bae5-4690-bc71-70aeabf01c1f"
EMPTY_LIST: dict[str, Any] = {
    "ok": True,
    "productScope": "evaluation_runs",
    "campaigns": [],
    "sessions": [],
    "tasks": [],
    "results": [],
}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Dharma:
    """MockTransport that records requests and answers by method."""

    def __init__(
        self,
        *,
        get: Callable[[httpx.Request], httpx.Response] | None = None,
        post: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._get = get or (lambda _r: httpx.Response(200, json=EMPTY_LIST))
        self._post = post or (
            lambda _r: httpx.Response(201, json=fixture("launch_201.json"))
        )

    def evaluator(self) -> EvaluatorDharma:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return (self._post if request.method == "POST" else self._get)(request)

        return EvaluatorDharma(
            base_url=BASE,
            org_id=ORG,
            token=SecretStr(TOKEN),
            transport=httpx.MockTransport(_handle),
        )

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def submission(**overrides: Any) -> ModelEvaluatorSubmission:
    fields: dict[str, Any] = {
        "idempotency_key": "onex-eval-full-00000000-0000-4000-8000-000000000001",
        "content_mode": "full",
        "prompt_text": "Write add(a, b) returning a + b.",
        "response_text": "def add(a, b):\n    return a - b\n",
    }
    fields.update(overrides)
    return ModelEvaluatorSubmission.model_validate(fields)


def reply(status: int, name: str | None = None, **kw: Any) -> Callable[..., Any]:
    return lambda _r: httpx.Response(status, json=fixture(name) if name else None, **kw)


# -- submit ------------------------------------------------------------------


async def test_submit_looks_up_then_launches_one_campaign() -> None:
    dharma = Dharma()
    receipt = await dharma.evaluator().submit(submission())
    assert receipt.receipt_id == fixture("launch_201.json")["campaignId"]
    assert receipt.duplicate is False
    lookup, launch = dharma.requests
    assert lookup.method == "GET"
    assert lookup.url.path == CAMPAIGNS
    assert lookup.url.params["limit"] == "100"
    assert launch.method == "POST"
    assert launch.url.path == CAMPAIGNS
    assert launch.headers["Authorization"] == f"Bearer {TOKEN}"
    assert launch.headers["Idempotency-Key"] == submission().idempotency_key
    body = json.loads(launch.content)
    assert body["integrationMode"] == "observe_existing_runtime"
    assert body["arms"] == ["direct_baseline"]
    assert body["name"] == f"onex-trajectory-{submission().idempotency_key}"
    (task,) = body["tasks"]
    assert task["task_id"] == submission().idempotency_key
    assert task["scenario"] == {
        "user_message": INSTRUCTIONS,
        "task_text": submission().prompt_text,
        "delegated_output": submission().response_text,
    }


async def test_submit_returns_the_existing_campaign_and_launches_nothing() -> None:
    # Dharma made a new billed campaign for a replayed Idempotency-Key in the
    # trial run, so the adapter finds the earlier one by its derived name.
    dharma = Dharma(get=reply(200, "campaign_completed_block.json"))
    receipt = await dharma.evaluator().submit(submission(idempotency_key=RECORDED_KEY))
    assert receipt.receipt_id == RECORDED_ID
    assert receipt.duplicate is True
    assert dharma.posts == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"content_mode": "meta"}, id="meta"),
        pytest.param({"prompt_text": None}, id="no_prompt"),
        pytest.param({"response_text": None}, id="no_response"),
    ],
)
async def test_submit_without_full_content_is_refused_before_any_call(
    overrides: dict[str, Any],
) -> None:
    dharma = Dharma()
    with pytest.raises(EvaluatorRejectedError):
        await dharma.evaluator().submit(submission(**overrides))
    assert dharma.requests == []


async def test_oversize_task_is_refused_not_truncated() -> None:
    dharma = Dharma()
    big = "x" * TASK_MAX_CHARS
    with pytest.raises(EvaluatorRejectedError) as info:
        await dharma.evaluator().submit(submission(response_text=big))
    assert info.value.status_code == 413
    assert dharma.requests == []


# -- status ------------------------------------------------------------------


async def test_running_campaign_is_not_terminal() -> None:
    dharma = Dharma(get=reply(200, "campaign_running.json"))
    status = await dharma.evaluator().read_status(RECORDED_ID)
    assert status.terminal is False
    assert status.verdict is None
    (read,) = dharma.requests
    assert read.url.params["campaignId"] == RECORDED_ID


async def test_completed_campaign_carries_judgment_and_dharma_grade() -> None:
    dharma = Dharma(get=reply(200, "campaign_completed_block.json"))
    status = await dharma.evaluator().read_status(RECORDED_ID)
    assert status.terminal is True
    assert status.verdict == (
        "judgment=block dharma=failed score=0.85 failed=evidence_boundary_preserved"
    )


async def test_completed_acceptable_campaign() -> None:
    body = fixture("campaign_completed_acceptable.json")
    dharma = Dharma(get=lambda _r: httpx.Response(200, json=body))
    status = await dharma.evaluator().read_status(body["campaigns"][0]["id"])
    assert status.terminal is True
    assert status.verdict is not None
    assert status.verdict.startswith("judgment=acceptable ")


async def test_unknown_campaign_is_rejected() -> None:
    dharma = Dharma()
    with pytest.raises(EvaluatorRejectedError) as info:
        await dharma.evaluator().read_status(RECORDED_ID)
    assert info.value.status_code == 404


@pytest.mark.parametrize("receipt", ["../x", "a b", "", "x" * 101])
async def test_malformed_receipt_id_is_refused_before_any_call(receipt: str) -> None:
    dharma = Dharma()
    with pytest.raises(EvaluatorRejectedError):
        await dharma.evaluator().read_status(receipt)
    assert dharma.requests == []


# -- errors ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "name", "error", "code"),
    [
        (401, "error_401_invalid_token.json", EvaluatorRejectedError, "invalid_token"),
        (403, "error_403_org_not_found.json", EvaluatorRejectedError, "org_not_found"),
        (
            403,
            "error_403_managed_runtime_requires_real_organization.json",
            EvaluatorRejectedError,
            "managed_runtime_requires_real_organization",
        ),
        (
            400,
            "error_400_invalid_contract.json",
            EvaluatorRejectedError,
            "invalid_evaluation_contract",
        ),
        (
            400,
            "synthetic_error_400_envelope.json",
            EvaluatorRejectedError,
            "invalid_request",
        ),
        (429, "synthetic_error_429.json", EvaluatorRateLimitedError, "rate_limited"),
        (503, "synthetic_error_503.json", EvaluatorTransportError, "unavailable"),
        (500, "synthetic_error_503.json", EvaluatorTransportError, "unavailable"),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_launch_errors_are_classified(
    status: int, name: str, error: type[EvaluatorError], code: str, method: str
) -> None:
    dharma = Dharma(**{method.lower(): reply(status, name)})
    with pytest.raises(error) as info:
        await dharma.evaluator().submit(submission())
    assert info.value.status_code == status
    assert info.value.error_code == code
    assert str(info.value) == f"Dharma status {status} {code}"
    assert TOKEN not in str(info.value)
    if method == "GET":
        assert dharma.posts == []


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "code",
    [None, "", "free text", "invalid\n", "a" * 101, "café", "Bearer " + TOKEN],
)
async def test_invalid_error_codes_are_not_exposed(
    code: str | None, nested: bool
) -> None:
    body = {
        "ok": False,
        "error": {"code": code, "message": TOKEN} if nested else code,
        "message": TOKEN,
    }
    dharma = Dharma(get=lambda _r: httpx.Response(403, json=body))
    with pytest.raises(EvaluatorRejectedError) as info:
        await dharma.evaluator().submit(submission())
    assert info.value.error_code is None
    assert str(info.value) == "Dharma status 403"
    assert dharma.posts == []


@pytest.mark.parametrize("code", ["A0_.-", "a" * 100])
async def test_valid_error_code_boundaries_are_preserved(code: str) -> None:
    dharma = Dharma(get=lambda _r: httpx.Response(403, json={"error": code}))
    with pytest.raises(EvaluatorRejectedError) as info:
        await dharma.evaluator().submit(submission())
    assert info.value.error_code == code
    assert str(info.value) == f"Dharma status 403 {code}"


async def test_402_trips_the_credit_stop() -> None:
    dharma = Dharma(post=reply(402, "synthetic_error_402_credits.json"))
    with pytest.raises(EvaluatorRejectedError) as info:
        await dharma.evaluator().submit(submission())
    assert info.value.credit_exhausted is True
    assert info.value.error_code == "insufficient_credits"


async def test_redirect_is_not_followed() -> None:
    dharma = Dharma(
        post=lambda _r: httpx.Response(
            302, headers={"Location": "https://elsewhere.test/"}
        )
    )
    with pytest.raises(EvaluatorTransportError):
        await dharma.evaluator().submit(submission())
    assert [r.url.host for r in dharma.requests] == ["dharma.example.test"] * 2


async def test_non_json_success_is_a_transport_error() -> None:
    dharma = Dharma(post=lambda _r: httpx.Response(201, text="<html>"))
    with pytest.raises(EvaluatorTransportError):
        await dharma.evaluator().submit(submission())


async def test_connection_failure_is_a_transport_error_without_the_token() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}")

    dharma = Dharma(get=_boom)
    with pytest.raises(EvaluatorTransportError) as info:
        await dharma.evaluator().submit(submission())
    assert TOKEN not in str(info.value)


# -- construction ------------------------------------------------------------


def test_host_is_the_base_url_host() -> None:
    assert Dharma().evaluator().host == "dharma.example.test"


@pytest.mark.parametrize(
    ("base_url", "org_id"),
    [
        ("http://dharma.example.test", ORG),
        ("dharma.example.test", ORG),
        (BASE, "../org"),
        (BASE, "x"),
    ],
)
def test_construction_refuses_bad_url_or_org(base_url: str, org_id: str) -> None:
    with pytest.raises(ValueError, match="Dharma"):
        EvaluatorDharma(base_url=base_url, org_id=org_id, token=SecretStr(TOKEN))


# -- through the handler -----------------------------------------------------


async def test_handler_builds_dharma_from_the_secret_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
        HandlerTrajectoryEvaluation,
    )
    from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
        ModelTrajectoryEvaluationAccepted,
    )
    from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
        SETTING,
        FakeClock,
        Github,
        full_cmd,
    )

    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    monkeypatch.setenv("DHARMA_API_KEY", TOKEN)
    monkeypatch.setenv("DHARMA_ORG_ID", ORG)
    launched = fixture("launch_201.json")["campaignId"]
    completed = fixture("campaign_completed_block.json")
    completed["campaigns"][0]["id"] = launched

    def _get(request: httpx.Request) -> httpx.Response:
        if "campaignId" in request.url.params:
            return httpx.Response(200, json=completed)
        return httpx.Response(200, json=EMPTY_LIST)

    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=fixture("launch_201.json"))
        return _get(request)

    github = Github()
    handler = HandlerTrajectoryEvaluation(
        transport=github.transport(),
        evaluator_transport=httpx.MockTransport(_handle),
        clock=FakeClock(),
        environ={SETTING: "dharma_full"},
    )
    out = await handler.handle(full_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationAccepted), out
    assert out.backend_receipt_id == launched
    assert out.verdict == (
        "judgment=block dharma=failed score=0.85 failed=evidence_boundary_preserved"
    )
    assert [r.method for r in seen] == ["GET", "POST", "GET"]
    assert all(r.url.path.startswith(f"/api/orgs/{ORG}/") for r in seen)
    assert seen[1].headers["Authorization"] == f"Bearer {TOKEN}"
