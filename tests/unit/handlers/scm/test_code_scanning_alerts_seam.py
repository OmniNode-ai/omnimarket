# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: ``onex scm code-scanning-alerts`` -> bus request -> gateway -> GitHub.

The fake bus answers a published gateway command with the real gateway dispatch
over a strict, ordered replay of GitHub's own answers. So the request the CLI
publishes, the request the gateway sends to GitHub, and the result the CLI
prints are one chain under test: a change to the request model, the ref the
gateway asks about, or the result shape on either side fails here.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.handlers.scm import scm_cli
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    ModelCodeScanningAlert,
    ModelCodeScanningAlertsResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    JsonExchange,
    RecordedGatewayTransport,
    RecordedTransportMismatchError,
    get,
    json_response,
)
from tests.unit.handlers.scm.test_onex_scm_command import (
    GATEWAY,
    REPO,
    FakeBus,
    _answer,
    _install,
    _run,
)

pytestmark = pytest.mark.unit

_ALERTS = f"/repos/{REPO}/code-scanning/alerts?state=open"
_PR_URL = f"{_ALERTS}&ref=refs%2Fpull%2F6096%2Fmerge&per_page=30"
_BRANCH_URL = f"{_ALERTS}&ref=refs%2Fheads%2Fdev&per_page=30"

_GITHUB_ALERT: dict[str, Any] = {
    "number": 7,
    "state": "open",
    "html_url": f"https://github.com/{REPO}/security/code-scanning/7",
    "rule": {"id": "py/path-injection", "severity": "error"},
    "most_recent_instance": {"location": {"path": "src/a.py", "start_line": 12}},
    "tool": {"name": "CodeQL"},
}


def _gateway_bus(transport: RecordedGatewayTransport) -> FakeBus:
    class GatewayBus(FakeBus):
        def _respond(
            self, topic: str, payload: dict[str, Any]
        ) -> tuple[str, dict[str, Any]]:
            assert topic == GATEWAY.command
            request = ModelGithubGatewayRequest.model_validate(payload)
            response = ModelGithubGatewayResponse(
                correlation_id=request.correlation_id,
                result=dispatch(request, transport),
            )
            return GATEWAY.success, response.model_dump(mode="json")

    return GatewayBus()


def test_pull_request_read_runs_cli_bus_gateway_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordedGatewayTransport(
        [JsonExchange(get(_PR_URL), json_response([_GITHUB_ALERT]))]
    )
    bus = _gateway_bus(transport)
    _install(monkeypatch, bus)

    result = _run(["code-scanning-alerts", f"{REPO}#6096"])

    assert result.exit_code == 0, result.output
    topic, envelope = bus.published[0]
    assert topic == GATEWAY.command
    sent = ModelGithubGatewayRequest.model_validate(envelope["payload"])
    assert (sent.operation.value, sent.repo) == ("code_scanning_alerts", REPO)
    assert (sent.pr_number, sent.branch) == (6096, None)
    transport.assert_drained()
    answer = _answer(result)
    assert answer["outcome"] == "completed"
    assert answer["result"]["operation"] == "code_scanning_alerts"
    assert answer["result"]["ref"] == "refs/pull/6096/merge"
    assert answer["result"]["alerts"] == [
        {
            "number": 7,
            "rule_id": "py/path-injection",
            "severity": "error",
            "security_severity": None,
            "path": "src/a.py",
            "line": 12,
            "html_url": f"https://github.com/{REPO}/security/code-scanning/7",
        }
    ]


def test_branch_read_runs_cli_bus_gateway_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordedGatewayTransport(
        [JsonExchange(get(_BRANCH_URL), json_response([]))]
    )
    bus = _gateway_bus(transport)
    _install(monkeypatch, bus)

    result = _run(["code-scanning-alerts", REPO, "--branch", "dev"])

    assert result.exit_code == 0, result.output
    sent = ModelGithubGatewayRequest.model_validate(bus.published[0][1]["payload"])
    assert (sent.pr_number, sent.branch) == (None, "dev")
    transport.assert_drained()
    answer = _answer(result)
    assert answer["result"]["ref"] == "refs/heads/dev"
    assert answer["result"]["alerts"] == []
    assert answer["result"]["available"] is True


def test_a_repository_with_no_analysis_is_a_completed_unavailable_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(_PR_URL),
                json_response({"message": "no analysis found"}, status=404),
            )
        ]
    )
    _install(monkeypatch, _gateway_bus(transport))

    result = _run(["code-scanning-alerts", f"{REPO}#6096"])

    assert result.exit_code == 0, result.output
    answer = _answer(result)
    assert answer["outcome"] == "completed"
    assert answer["result"]["available"] is False
    assert answer["result"]["unavailable_reason"] == "no_analysis"
    assert answer["result"]["alerts"] == []


def test_the_result_and_alert_shapes_are_pinned() -> None:
    assert set(ModelCodeScanningAlert.model_fields) == {
        "number",
        "rule_id",
        "severity",
        "security_severity",
        "path",
        "line",
        "html_url",
    }
    assert set(ModelCodeScanningAlertsResult.model_fields) == {
        "operation",
        "repo",
        "ref",
        "available",
        "unavailable_reason",
        "alerts",
        "total_count",
        "truncated",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["code-scanning-alerts", REPO],
        ["code-scanning-alerts", f"{REPO}#6096", "--branch", "dev"],
    ],
)
def test_a_target_naming_neither_or_both_is_a_usage_error_with_no_publish(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)

    outcome = CliRunner().invoke(scm_cli.scm_group, argv)

    assert outcome.exit_code != 0
    assert "exactly one of pr_number and branch" in outcome.output
    assert bus.published == []


def test_control_a_drifted_ref_fails_the_strict_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: replay recorded for the branch ref cannot answer a pull request read.

    If the seam test did not compare the request the gateway sends to GitHub, a
    gateway that asked about the wrong ref would still pass; here it fails.
    """
    transport = RecordedGatewayTransport(
        [JsonExchange(get(_BRANCH_URL), json_response([]))]
    )
    _install(monkeypatch, _gateway_bus(transport))

    with pytest.raises(RecordedTransportMismatchError):
        _run(["code-scanning-alerts", f"{REPO}#6096"])


def test_control_the_request_model_refuses_neither_and_both() -> None:
    with pytest.raises(ValidationError, match="exactly one of pr_number and branch"):
        ModelGithubGatewayRequest(operation="code_scanning_alerts", repo=REPO)
    with pytest.raises(ValidationError, match="exactly one of pr_number and branch"):
        ModelGithubGatewayRequest(
            operation="code_scanning_alerts", repo=REPO, pr_number=1, branch="dev"
        )
