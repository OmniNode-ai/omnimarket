# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A node that refuses a request field the client sends is named as older than the client.

The refusal text is the one the dev gateway node answered on 2026-11-06 for every
read once the client carried ``cache_only``.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pydantic import JsonValue

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.handler_gateway_read_client import (
    read_over_bus,
)
from omnimarket.models import model_gateway_node_skew as skew
from omnimarket.models.model_gateway_node_skew import (
    GatewayNodeOlderError,
    GatewayReadError,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from tests.unit.handlers.scm.test_handler_gateway_read_client import REPO, _install
from tests.unit.handlers.scm.test_onex_scm_command import GATEWAY, FakeBus
from tests.unit.handlers.scm.test_onex_scm_command import _install as _install_cli

pytestmark = pytest.mark.unit

_EXTRA = (
    "1 validation error for ModelGithubGatewayRequest\n"
    "cache_only\n"
    "  Extra inputs are not permitted [type=extra_forbidden, input_value=False, "
    "input_type=bool]\n"
    "    For further information visit https://errors.pydantic.dev/2.11/v/extra_forbidden"
)
_OTHER = (
    "1 validation error for ModelGithubGatewayRequest\n"
    "operation\n"
    "  Input should be 'pr_status' [type=enum, input_value='merged_pr', "
    "input_type=str]"
)
_MIXED = (
    "2 validation errors for ModelGithubGatewayRequest\n"
    "cache_only\n"
    "  Extra inputs are not permitted [type=extra_forbidden, input_value=False, "
    "input_type=bool]\n"
    "repo\n"
    "  Field required [type=missing, input_value={}, input_type=dict]"
)


def _payload(
    reason: str, failure_class: str = "ValidationError"
) -> dict[str, JsonValue]:
    return {
        "status": "failed",
        "failure_class": failure_class,
        "failure_reason": reason,
    }


def _refuse_with(bus: FakeBus, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    def respond(
        topic: str, payload: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        return GATEWAY.success, {
            "correlation_id": payload["correlation_id"],
            **_payload(reason),
        }

    monkeypatch.setattr(bus, "_respond", respond)


def test_the_refused_field_is_read_from_the_real_refusal_text() -> None:
    assert skew.refused_fields(_payload(_EXTRA)) == ("cache_only",)


@pytest.mark.parametrize(
    "payload",
    [
        _payload(_OTHER),
        _payload(_MIXED),
        _payload(_EXTRA, failure_class="RuntimeError"),
        {"status": "failed"},
    ],
)
def test_any_other_failure_is_not_reported_as_node_skew(
    payload: dict[str, JsonValue],
) -> None:
    assert skew.refused_fields(payload) == ()


def test_a_read_over_the_bus_says_the_node_is_older_than_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _refuse_with(bus, monkeypatch, _EXTRA)
    _install(monkeypatch, bus)

    with pytest.raises(GatewayNodeOlderError) as caught:
        read_over_bus(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.MERGED_PR,
                repo=REPO,
                pr_number=77,
            )
        )
    text = str(caught.value)
    assert "older than this client" in text
    assert "cache_only" in text
    assert "dev-merge tag" in text


def test_a_different_validation_failure_stays_an_ordinary_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _refuse_with(bus, monkeypatch, _OTHER)
    _install(monkeypatch, bus)

    with pytest.raises(GatewayReadError) as caught:
        read_over_bus(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.MERGED_PR,
                repo=REPO,
                pr_number=77,
            )
        )
    assert not isinstance(caught.value, GatewayNodeOlderError)
    assert "older than this client" not in str(caught.value)


def test_onex_scm_prints_the_plain_sentence_and_names_the_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _refuse_with(bus, monkeypatch, _EXTRA)
    _install_cli(monkeypatch, bus)

    result = CliRunner().invoke(scm_cli.scm_group, ["release-list", REPO])

    assert result.exit_code == scm_cli.EXIT_FAILED, result.output
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    assert answer["outcome"] == "failed"
    assert answer["result"]["node_older_than_client"]["refused_fields"] == [
        "cache_only"
    ]
    assert "older than this client" in result.stderr
