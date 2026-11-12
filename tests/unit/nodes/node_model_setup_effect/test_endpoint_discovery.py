# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Discovery controls: arbitrary IDs, auth isolation and hostile responses.

Failure modes: a built-in model allowlist hides server models; credentials leak
via URLs/errors; a partial or failed response masquerades as a current catalog;
listing invokes inference/provisioning; endpoint identity is lost between reads.
"""

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from pydantic import SecretStr, ValidationError

from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
)
from omnimarket.nodes.node_model_setup_effect.cli_catalog import discover_command
from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_models import (
    HandlerEndpointModels,
)

pytestmark = pytest.mark.unit


def test_all_server_models_and_order_are_preserved_without_preference_filter() -> None:
    calls: list[dict[str, object]] = []

    def get(**kwargs: object) -> object:
        calls.append(kwargs)
        return {"data": [{"id": "new-model:custom"}, {"id": "org/experimental"}]}

    result = HandlerEndpointModels(get=get).handle(
        ModelEndpointModelsRequest(url="https://models.example.test/team/v1/")
    )
    assert result.status == "ready"
    assert result.models == ("new-model:custom", "org/experimental")
    assert calls == [
        {
            "url": "https://models.example.test/team/v1/models",
            "timeout_seconds": 15.0,
            "extra_headers": {},
        }
    ]


def test_credentials_belong_only_to_the_explicit_connection() -> None:
    calls: list[dict[str, object]] = []

    def get(**kwargs: object) -> object:
        calls.append(kwargs)
        return {"data": [{"id": "arbitrary-model"}]}

    handler = HandlerEndpointModels(get=get)
    for host, key in (("one", "secret-one"), ("two", "secret-two"), ("three", None)):
        request = ModelEndpointModelsRequest(
            url=f"https://{host}.example.test", key=SecretStr(key) if key else None
        )
        result = handler.handle(request)
        assert result.url == f"https://{host}.example.test"
        assert "secret-" not in result.model_dump_json() + request.model_dump_json()
    assert [c["extra_headers"] for c in calls] == [
        {"Authorization": "Bearer secret-one"},
        {"Authorization": "Bearer secret-two"},
        {},
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test",
        "https://example.test?key=secret",
        "https://example.test/#secret",
        "file:///tmp/models",
        "https://example.test/\nsecret",
    ],
)
def test_unsafe_endpoint_is_refused_before_network(url: str) -> None:
    with pytest.raises(ValidationError):
        ModelEndpointModelsRequest(url=url)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (401, "rejected"),
        (403, "rejected"),
        (404, "unsupported"),
        (405, "unsupported"),
        (429, "unavailable"),
        (500, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_http_failures_do_not_become_model_lists_or_echo_secrets(
    code: int, status: str
) -> None:
    def get(**_kwargs: object) -> object:
        response = httpx.Response(
            code,
            request=httpx.Request("GET", "https://example.test"),
            text="secret-SENTINEL",
        )
        response.raise_for_status()
        raise AssertionError("expected a failing HTTP status")

    result = HandlerEndpointModels(get=get).handle(
        ModelEndpointModelsRequest(
            url="https://example.test", key=SecretStr("secret-SENTINEL")
        )
    )
    assert result.status == status
    assert result.models == ()
    assert "SENTINEL" not in result.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": {}},
        {"data": [{"id": "ok"}, {}]},
        {"data": [{"id": "secret-SENTINEL"}]},
    ],
)
def test_malformed_or_secret_bearing_catalog_never_returns_a_partial_list(
    payload: object,
) -> None:
    result = HandlerEndpointModels(get=lambda **_: payload).handle(
        ModelEndpointModelsRequest(
            url="https://example.test", key=SecretStr("secret-SENTINEL")
        )
    )
    assert result.status == "invalid"
    assert result.models == ()
    assert "SENTINEL" not in result.model_dump_json()


def test_catalog_cli_json_contains_all_ids_without_banner_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers import (
        handler_endpoint_models,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        handler_endpoint_models,
        "get_models_json",
        lambda **_: {"data": [{"id": "server-only:latest"}]},
    )
    result = CliRunner().invoke(
        discover_command, ["--url", "https://example.test", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("{")
    assert "server-only:latest" in result.output
    assert not list(tmp_path.iterdir())
