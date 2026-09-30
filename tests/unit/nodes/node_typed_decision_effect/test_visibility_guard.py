# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed-decision node's repository-visibility guard (OMN-20149).

The guard reads ``GET /repos/<owner>/<name>`` before anything reaches the
third-party decision backend. Anonymous, that read is capped at 60 an hour per
address, so a burst of decisions turned into ``repository_visibility_unresolved``
refusals that read as model failures (368 of 447 calls in the Jev usage eval).
The read now carries a token when one resolves, and a real 200 that reports a
repository public is remembered for a TTL, in process and on disk.

What must not change: a private, absent or unresolvable repository is refused,
and only a real 200 with ``private: false`` is ever remembered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.config.service_endpoints import GITHUB_REST_URL
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    EnumTypedDecisionKind,
    EnumTypedDecisionReason,
    ModelTypedDecisionRequest,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BIFROST = _REPO_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_NODE_DIR = _REPO_ROOT / "src/omnimarket/nodes/node_typed_decision_effect"
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_TTL = _CONTRACT["work_unit_scoping"]["visibility_cache_ttl_seconds"]
_BACKEND = next(
    b
    for b in yaml.safe_load(_BIFROST.read_text(encoding="utf-8"))["backends"]
    if b["backend_id"] == _CONTRACT["decision_routing"]["backend_id"]
)
_FAKE_KEY = "fake-typesafe-key-for-tests"
_FAKE_GH_TOKEN = "ghp_fake_token_for_tests"
_PUBLIC = "OmniNode-ai/public-repo"
_OTHER_PUBLIC = "OmniNode-ai/other-public-repo"
_PRIVATE = "OmniNode-ai/private-repo"


class _Store:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get_secret(self, key: str) -> str | None:
        return self._values.get(key)


class _Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Transport:
    """GitHub visibility reads plus the decision endpoint, both recorded."""

    def __init__(self) -> None:
        self.visibility: list[httpx.Request] = []
        self.decisions: list[httpx.Request] = []
        self.public = {_PUBLIC, _OTHER_PUBLIC}
        self.private_but_visible: set[str] = set()
        self.visibility_status_override: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{GITHUB_REST_URL}/repos/"):
            self.visibility.append(request)
            if self.visibility_status_override:
                status = self.visibility_status_override.pop(0)
                return httpx.Response(status, json={"message": "no"})
            repo = url.removeprefix(f"{GITHUB_REST_URL}/repos/")
            if repo in self.public:
                return httpx.Response(200, json={"private": False})
            if repo in self.private_but_visible:
                return httpx.Response(200, json={"private": True})
            return httpx.Response(404, json={"message": "Not Found"})
        if url == _BACKEND["endpoint_url"]:
            self.decisions.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "jev-9.9.9",
                    "answers": {
                        "decision": {
                            "type": "choice",
                            "choice": "change",
                            "probabilities": {"change": 0.95, "infrastructure": 0.05},
                            "confidence": 0.95,
                        }
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            )
        raise AssertionError(f"unexpected request to {url}")


def _handler(
    transport: _Transport,
    tmp: Path,
    *,
    clock: _Clock | None = None,
    cache_path: Path | None = None,
    github_token: str | None = None,
) -> HandlerTypedDecision:
    values = {_BACKEND["secret_ref"]: _FAKE_KEY}
    if github_token is not None:
        values["GITHUB_TOKEN"] = github_token
    return HandlerTypedDecision(
        transport=httpx.MockTransport(transport),
        secret_store=cast(ProtocolSecretStore, _Store(values)),
        bifrost_config_path=_BIFROST,
        bifrost_overlay_path=tmp / "no-overlay.yaml",
        visibility_cache_path=cache_path,
        clock=clock or _Clock(),
    )


def _ask(repo: str) -> ModelTypedDecisionRequest:
    return ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=repo,
        state={"ci_failure": "ruff format check failed"},
        kind=EnumTypedDecisionKind.CHOICE,
        instructions="Is this CI failure caused by the change, or is it infrastructure?",
        criteria={"change": "The diff caused it", "infrastructure": "Runner"},
        incumbent_answer=None,
    )


@pytest.mark.unit
def test_the_contract_declares_the_ttl_and_an_optional_github_token() -> None:
    assert isinstance(_TTL, int)
    assert 0 < _TTL <= 86400
    token = _CONTRACT["secrets"]["GITHUB_TOKEN"]
    assert token["required"] is False


@pytest.mark.unit
def test_a_public_repository_is_read_once_inside_the_ttl(tmp_path: Path) -> None:
    transport = _Transport()
    clock = _Clock()
    handler = _handler(transport, tmp_path, clock=clock)

    results = []
    for _ in range(5):
        clock.now += 1
        results.append(handler.handle(_ask(_PUBLIC)))

    assert all(r.decided_by is EnumTypedDecisionDecider.MODEL for r in results)
    assert len(transport.visibility) == 1
    assert len(transport.decisions) == 5


@pytest.mark.unit
def test_the_read_is_repeated_once_the_ttl_has_passed(tmp_path: Path) -> None:
    transport = _Transport()
    clock = _Clock()
    handler = _handler(transport, tmp_path, clock=clock)

    handler.handle(_ask(_PUBLIC))
    clock.now += _TTL - 1
    handler.handle(_ask(_PUBLIC))
    assert len(transport.visibility) == 1
    clock.now += 2
    handler.handle(_ask(_PUBLIC))
    assert len(transport.visibility) == 2


@pytest.mark.unit
def test_a_cached_public_repository_admits_no_other_repository(
    tmp_path: Path,
) -> None:
    """Positive control for the cache: it admits the repository it read and
    nothing else, so a cache that admits everything cannot pass."""
    transport = _Transport()
    handler = _handler(transport, tmp_path)

    assert handler.handle(_ask(_PUBLIC)).decided_by is EnumTypedDecisionDecider.MODEL
    refused = handler.handle(_ask(_PRIVATE))

    assert refused.decided_by is EnumTypedDecisionDecider.NO_ANSWER
    assert refused.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert len(transport.decisions) == 1
    assert len(transport.visibility) == 2


@pytest.mark.unit
def test_a_refusal_is_never_cached(tmp_path: Path) -> None:
    transport = _Transport()
    handler = _handler(transport, tmp_path)

    first = handler.handle(_ask(_PRIVATE))
    second = handler.handle(_ask(_PRIVATE))

    assert first.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert second.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert len(transport.visibility) == 2
    assert transport.decisions == []


@pytest.mark.unit
@pytest.mark.parametrize("status", [403, 429, 500])
def test_a_rate_limited_read_is_refused_then_read_again_not_remembered(
    tmp_path: Path, status: int
) -> None:
    transport = _Transport()
    transport.visibility_status_override = [status]
    handler = _handler(transport, tmp_path)

    limited = handler.handle(_ask(_PUBLIC))
    recovered = handler.handle(_ask(_PUBLIC))

    assert limited.reason is EnumTypedDecisionReason.REPOSITORY_VISIBILITY_UNRESOLVED
    assert recovered.decided_by is EnumTypedDecisionDecider.MODEL
    assert len(transport.visibility) == 2
    # Only the recovered call reached the backend.
    assert len(transport.decisions) == 1


@pytest.mark.unit
def test_a_credential_that_can_see_a_private_repository_still_refuses_it(
    tmp_path: Path,
) -> None:
    """An authenticated read returns 200 for a private repository the token can
    see. The guard reads the ``private`` field, so 200 alone never admits."""
    transport = _Transport()
    transport.private_but_visible = {_PRIVATE}
    handler = _handler(transport, tmp_path, github_token=_FAKE_GH_TOKEN)

    result = handler.handle(_ask(_PRIVATE))
    again = handler.handle(_ask(_PRIVATE))

    assert result.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert again.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert transport.decisions == []
    assert len(transport.visibility) == 2


@pytest.mark.unit
def test_the_read_carries_the_token_when_one_resolves(tmp_path: Path) -> None:
    transport = _Transport()
    handler = _handler(transport, tmp_path, github_token=_FAKE_GH_TOKEN)

    result = handler.handle(_ask(_PUBLIC))

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    (read,) = transport.visibility
    assert read.headers["Authorization"] == f"Bearer {_FAKE_GH_TOKEN}"
    (decision,) = transport.decisions
    assert _FAKE_GH_TOKEN not in decision.headers.get("Authorization", "")
    assert _FAKE_GH_TOKEN not in result.model_dump_json()


@pytest.mark.unit
def test_the_read_is_anonymous_when_no_token_resolves(tmp_path: Path) -> None:
    transport = _Transport()
    handler = _handler(transport, tmp_path)

    handler.handle(_ask(_PUBLIC))

    (read,) = transport.visibility
    assert "Authorization" not in read.headers


@pytest.mark.unit
def test_a_rejected_token_is_a_refusal_not_a_silent_anonymous_retry(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    transport.visibility_status_override = [401]
    handler = _handler(transport, tmp_path, github_token=_FAKE_GH_TOKEN)

    result = handler.handle(_ask(_PUBLIC))

    assert result.reason is EnumTypedDecisionReason.REPOSITORY_VISIBILITY_UNRESOLVED
    assert transport.decisions == []
    assert len(transport.visibility) == 1


@pytest.mark.unit
def test_the_disk_cache_serves_a_new_process_and_holds_no_secret(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "state" / "visibility.json"
    first_transport = _Transport()
    clock = _Clock()
    _handler(
        first_transport,
        tmp_path,
        clock=clock,
        cache_path=cache,
        github_token=_FAKE_GH_TOKEN,
    ).handle(_ask(_PUBLIC))
    assert len(first_transport.visibility) == 1

    second_transport = _Transport()
    clock.now += 5
    result = _handler(second_transport, tmp_path, clock=clock, cache_path=cache).handle(
        _ask(_PUBLIC)
    )

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert second_transport.visibility == []
    text = cache.read_text(encoding="utf-8")
    assert _FAKE_GH_TOKEN not in text
    assert _FAKE_KEY not in text
    assert set(json.loads(text)["public"]) == {_PUBLIC.lower()}


@pytest.mark.unit
def test_the_disk_cache_expires_with_the_ttl(tmp_path: Path) -> None:
    cache = tmp_path / "visibility.json"
    clock = _Clock()
    _handler(_Transport(), tmp_path, clock=clock, cache_path=cache).handle(
        _ask(_PUBLIC)
    )

    later = _Transport()
    clock.now += _TTL + 1
    _handler(later, tmp_path, clock=clock, cache_path=cache).handle(_ask(_PUBLIC))

    assert len(later.visibility) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        json.dumps(["a list"]),
        json.dumps({"version": 1, "public": ["x"]}),
        json.dumps({"version": 1, "public": {_PUBLIC.lower(): "yesterday"}}),
        json.dumps({"version": 1, "public": {_PUBLIC.lower(): True}}),
        # Future-dated: a clock that ran ahead must not extend a stale answer.
        json.dumps({"version": 1, "public": {_PUBLIC.lower(): 9_999_999_999.0}}),
    ],
)
def test_a_corrupt_or_future_dated_cache_file_is_ignored(
    tmp_path: Path, content: str
) -> None:
    cache = tmp_path / "visibility.json"
    cache.write_text(content, encoding="utf-8")
    transport = _Transport()

    result = _handler(transport, tmp_path, cache_path=cache).handle(_ask(_PUBLIC))

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert len(transport.visibility) == 1


@pytest.mark.unit
def test_an_unwritable_cache_path_never_blocks_a_decision(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    transport = _Transport()

    result = _handler(
        transport, tmp_path, cache_path=blocker / "cannot" / "visibility.json"
    ).handle(_ask(_PUBLIC))

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
