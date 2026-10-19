# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16987: provider-rung canary acceptance tests over a fake transport."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_provider_rung_canary_effect.handlers.handler_provider_rung_canary import (
    INVALID_KEY,
    REDACTED,
    HandlerProviderRungCanary,
    _save_state,
)
from omnimarket.nodes.node_provider_rung_canary_effect.models.model_provider_rung_canary import (
    EnumProviderRungVerdict as V,
)
from omnimarket.nodes.node_provider_rung_canary_effect.models.model_provider_rung_canary import (
    ModelProviderRungCanaryRequest,
    ModelProviderRungSuppression,
)

_COMMITTED = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)
GLM_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SENTINEL = "sk-SENTINEL-rung-canary-9f3a7c1e5b2d4086"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

OK_BODY: dict[str, Any] = {
    "model": "m",
    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
}


def _contract(tmp_path: Path, endpoints: dict[str, str]) -> str:
    """The committed contract with only ``endpoints`` carrying an endpoint_url."""
    data = yaml.safe_load(_COMMITTED.read_text(encoding="utf-8"))
    for backend in data["backends"]:
        backend["endpoint_url"] = endpoints.get(backend["backend_id"])
    path = tmp_path / "bifrost_delegation.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


class FakeTransport:
    """Scripted ``post``: answers by URL, records every request."""

    def __init__(self, answers: dict[str, dict[str, Any]] | None = None) -> None:
        self.answers = answers or {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        self.requests.append((url, headers))
        if headers["Authorization"] == f"Bearer {INVALID_KEY}":
            return {"http_status": 401, "latency_ms": 1}
        return dict(
            self.answers.get(
                url,
                {
                    "http_status": 200,
                    "latency_ms": 5,
                    "body_is_chat_completion": True,
                    "model_echo": "m",
                },
            )
        )


def _resolver(known: dict[str, str]) -> Any:
    async def resolve(ref: str, env_fallback: str | None) -> str | None:
        return known.get(ref)

    return resolve


def _request(path: str, **kw: Any) -> ModelProviderRungCanaryRequest:
    return ModelProviderRungCanaryRequest(contract_path=path, now=NOW, **kw)


KEYS = {
    "llm.glm.api_key": SENTINEL + "-glm",
    "llm.gemini.api_key": SENTINEL + "-gem",
    "llm.openrouter.api_key": SENTINEL + "-or",
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac1_n_credentialed_backends_give_exactly_n_probed_verdicts(
    tmp_path: Path,
) -> None:
    ends = {
        "cloud-glm": GLM_URL,
        "cloud-glm-5-3": GLM_URL,
        "cloud-gemini-flash": GEMINI_URL,
        "openrouter-nemotron-ultra": OPENROUTER_URL,
    }
    post = FakeTransport()
    result = await HandlerProviderRungCanary(_resolver(KEYS), post).handle(
        _request(_contract(tmp_path, ends))
    )
    probed = [r for r in result.rungs if r.probed]
    assert sorted(r.backend_id for r in probed) == sorted(ends)
    assert all(r.verdict is V.LIVE for r in probed)
    # Every declared backend is reported: the rest are skipped, never silently dropped.
    assert len(result.rungs) == 13
    skipped = {r.verdict for r in result.rungs if not r.probed}
    assert skipped == {V.SKIPPED_NO_ENDPOINT}
    # glm-5.3 and glm-5.3-flash differ by model, so 4 rungs are 4 requests (+ 3 controls).
    real = [h for _u, h in post.requests if INVALID_KEY not in h["Authorization"]]
    assert len(real) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac1_backends_sharing_a_rung_share_one_request(tmp_path: Path) -> None:
    ends = {"cloud-gemini-judge": GEMINI_URL, "cloud-gemini-2-5-flash": GEMINI_URL}
    post = FakeTransport()
    result = await HandlerProviderRungCanary(_resolver(KEYS), post).handle(
        _request(_contract(tmp_path, ends), wrong_key_control=False)
    )
    assert [r.verdict for r in result.rungs if r.probed] == [V.LIVE, V.LIVE]
    assert len(post.requests) == 1
    shared = {r.backend_id: r.shared_probe_with for r in result.rungs if r.probed}
    assert shared["cloud-gemini-judge"] == ("cloud-gemini-2-5-flash",)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {
                "http_status": 200,
                "body_is_chat_completion": True,
                "model_echo": "m",
            },
            V.LIVE,
        ),
        ({"http_status": 401}, V.AUTH_DEAD),
        ({"http_status": 403}, V.AUTH_DEAD),
        (
            {
                "http_status": 400,
                "provider_error_status": "INVALID_ARGUMENT",
                "provider_error_message": "Please pass a valid API key.",
            },
            V.AUTH_DEAD,
        ),
        ({"http_status": 429, "body": {}}, V.QUOTA_DEAD),
        ({"exception": "ConnectError", "exception_family": "transport"}, V.UNREACHABLE),
        ({"http_status": 200, "body_is_chat_completion": False}, V.PROTOCOL_ERROR),
        (
            {"exception": "JSONDecodeError", "exception_family": "decode"},
            V.PROTOCOL_ERROR,
        ),
        ({"http_status": 503}, V.HTTP_ERROR),
    ],
)
async def test_ac2_transport_outcome_maps_to_typed_verdict(
    tmp_path: Path, answer: dict[str, Any], expected: V
) -> None:
    ends = {"cloud-gemini-flash": GEMINI_URL}
    result = await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport({GEMINI_URL: answer})
    ).handle(_request(_contract(tmp_path, ends)))
    [rung] = [r for r in result.rungs if r.probed]
    assert rung.verdict is expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac3_unresolvable_secret_ref_gives_unresolved_and_no_request(
    tmp_path: Path,
) -> None:
    ends = {"cloud-glm": GLM_URL, "cloud-gemini-flash": GEMINI_URL}
    post = FakeTransport()
    keys = {"llm.gemini.api_key": SENTINEL}  # no glm key
    result = await HandlerProviderRungCanary(_resolver(keys), post).handle(
        _request(_contract(tmp_path, ends))
    )
    by_id = {r.backend_id: r for r in result.rungs}
    assert by_id["cloud-glm"].verdict is V.UNRESOLVED
    assert by_id["cloud-glm"].probed is True
    assert by_id["cloud-glm"].request_sent is False
    assert GLM_URL not in [u for u, _h in post.requests]
    assert by_id["cloud-gemini-flash"].verdict is V.LIVE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac3_a_resolver_that_raises_is_unresolved_not_a_crash(
    tmp_path: Path,
) -> None:
    async def boom(ref: str, env: str | None) -> str | None:
        raise RuntimeError("store down")

    post = FakeTransport()
    result = await HandlerProviderRungCanary(boom, post).handle(
        _request(_contract(tmp_path, {"cloud-glm": GLM_URL}))
    )
    [rung] = [r for r in result.rungs if r.probed]
    assert rung.verdict is V.UNRESOLVED
    assert post.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac4_no_key_material_in_verdict_log_or_summary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    echoed = {
        GLM_URL: {
            "http_status": 401,
            "provider_error_message": f"bad key {KEYS['llm.glm.api_key']} rejected",
        },
        GEMINI_URL: {
            "http_status": 200,
            "body_is_chat_completion": True,
            "model_echo": f"m-{KEYS['llm.gemini.api_key']}",
        },
    }
    ends = {"cloud-glm": GLM_URL, "cloud-gemini-flash": GEMINI_URL}
    caplog.set_level(logging.DEBUG)
    result = await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport(echoed)
    ).handle(_request(_contract(tmp_path, ends)))
    blob = result.model_dump_json() + result.summary_table + caplog.text
    assert SENTINEL not in blob
    assert result.key_echoes >= 2
    assert result.all_live is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac4_wrong_key_control_bites_and_all_live_needs_it(
    tmp_path: Path,
) -> None:
    ends = {"cloud-gemini-flash": GEMINI_URL}
    result = await HandlerProviderRungCanary(_resolver(KEYS), FakeTransport()).handle(
        _request(_contract(tmp_path, ends))
    )
    assert [c.verdict for c in result.controls] == [V.AUTH_DEAD]
    assert result.all_live is True

    class AlwaysOk(FakeTransport):
        def __call__(self, url: str, payload: Any, headers: Any, timeout: float) -> Any:
            return {
                "http_status": 200,
                "body_is_chat_completion": True,
                "model_echo": "m",
            }

    blind = await HandlerProviderRungCanary(_resolver(KEYS), AlwaysOk()).handle(
        _request(_contract(tmp_path, ends))
    )
    assert [c.verdict for c in blind.controls] == [V.LIVE]
    assert blind.all_live is False


ZAI_1310 = {
    "http_status": 429,
    "provider_error_code": "1310",
    "body": {
        "error": {
            "code": "1310",
            "message": "Weekly limit reached. Your limit will reset at 2026-10-19 08:00:00",
        }
    },
}
ZAI_1113 = {
    "http_status": 429,
    "provider_error_code": "1113",
    "body": {"error": {"code": "1113", "message": "Insufficient balance"}},
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac5_zai_1310_and_1113_are_distinguished(tmp_path: Path) -> None:
    ends = {"cloud-glm": GLM_URL}
    capped = await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport({GLM_URL: ZAI_1310})
    ).handle(_request(_contract(tmp_path, ends)))
    unfunded = await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport({GLM_URL: ZAI_1113})
    ).handle(_request(_contract(tmp_path, ends)))
    [a] = [r for r in capped.rungs if r.probed]
    [b] = [r for r in unfunded.rungs if r.probed]
    assert (a.verdict, b.verdict) == (V.QUOTA_DEAD, V.QUOTA_DEAD)
    assert (a.provider_error_code, b.provider_error_code) == ("1310", "1113")
    assert a.quota_class == "disable_until_reset"
    assert b.quota_class == "disable_until_billing"
    assert a.suppressed_until == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert b.suppressed_until is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ac5_1310_with_reset_suppresses_reprobe_in_window(tmp_path: Path) -> None:
    ends = {"cloud-glm": GLM_URL}
    contract = _contract(tmp_path, ends)
    state = str(tmp_path / "state.json")
    first = FakeTransport({GLM_URL: ZAI_1310})
    await HandlerProviderRungCanary(_resolver(KEYS), first).handle(
        _request(contract, state_path=state)
    )
    assert any(INVALID_KEY not in h["Authorization"] for _u, h in first.requests)

    second = FakeTransport({GLM_URL: ZAI_1310})
    inside = await HandlerProviderRungCanary(_resolver(KEYS), second).handle(
        _request(contract, state_path=state)
    )
    assert second.requests == []
    [rung] = [r for r in inside.rungs if r.backend_id == "cloud-glm"]
    assert rung.verdict is V.SKIPPED_SUPPRESSED
    assert rung.suppressed_until == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)

    third = FakeTransport()
    after = ModelProviderRungCanaryRequest(
        contract_path=contract,
        state_path=state,
        now=datetime(2026, 10, 19, 8, 0, tzinfo=UTC) + timedelta(seconds=1),
    )
    result = await HandlerProviderRungCanary(_resolver(KEYS), third).handle(after)
    assert [r.verdict for r in result.rungs if r.backend_id == "cloud-glm"] == [V.LIVE]
    assert result.suppressions == ()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting", [False, True])
async def test_state_file_holds_no_key_material_and_is_owner_only(
    tmp_path: Path, preexisting: bool
) -> None:
    ends = {"cloud-glm": GLM_URL}
    state = tmp_path / "state.json"
    if preexisting:
        state.write_text("[]", encoding="utf-8")
        os.chmod(state, 0o700)  # a preexisting mode the handler must replace with 0o600
    answer = {
        **ZAI_1310,
        "body": {
            "error": {
                **ZAI_1310["body"]["error"],
                "message": "Weekly limit reached for key "
                + KEYS["llm.glm.api_key"]
                + ". Your limit will reset at 2026-10-19 08:00:00",
            }
        },
    }
    await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport({GLM_URL: answer})
    ).handle(_request(_contract(tmp_path, ends), state_path=str(state)))

    assert state.exists()
    text = state.read_text(encoding="utf-8")
    assert SENTINEL not in text
    rows = json.loads(text)
    assert isinstance(rows, list)
    assert all(set(row) == {"backend_id", "until", "quota_code"} for row in rows)
    [glm] = [row for row in rows if row["backend_id"] == "cloud-glm"]
    assert datetime.fromisoformat(glm["until"]) == datetime(
        2026, 10, 19, 8, 0, tzinfo=UTC
    )
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o600


@pytest.mark.unit
def test_state_field_holding_a_resolved_value_is_written_redacted(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    rows = (
        ModelProviderRungSuppression(
            backend_id="cloud-glm", until=NOW, quota_code=f"1310 {SENTINEL}"
        ),
        ModelProviderRungSuppression(backend_id="cloud-gemini", until=NOW),
    )
    _save_state(str(state), rows, {SENTINEL})

    text = state.read_text(encoding="utf-8")
    assert SENTINEL not in text
    by_id = {row["backend_id"]: row for row in json.loads(text)}
    assert by_id["cloud-glm"]["quota_code"] == REDACTED
    assert by_id["cloud-gemini"]["quota_code"] is None
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o600


@pytest.mark.unit
@pytest.mark.asyncio
async def test_1113_is_not_suppressed(tmp_path: Path) -> None:
    ends = {"cloud-glm": GLM_URL}
    contract = _contract(tmp_path, ends)
    state = str(tmp_path / "state.json")
    await HandlerProviderRungCanary(
        _resolver(KEYS), FakeTransport({GLM_URL: ZAI_1113})
    ).handle(_request(contract, state_path=state))
    second = FakeTransport({GLM_URL: ZAI_1113})
    await HandlerProviderRungCanary(_resolver(KEYS), second).handle(
        _request(contract, state_path=state)
    )
    assert second.requests != []
