# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's code_scanning_alerts read on a recorded fake transport.

Golden chain: a pull request's merge ref and a branch each send exactly the
recorded GitHub request, page inside the node, honour the limit, and return the
typed alerts. Error chain: no analysis, scanning off and a token that cannot
read alerts are typed ``available=False`` answers with a reason; any other
failure raises. Failing control: a request naming neither or both of a pull
request and a branch is refused by the request model.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from omnimarket.models.github_landing.model_transport_error import GitHubTransportError
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCodeScanningAlertsResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
)

pytestmark = pytest.mark.unit

_BASE = f"/repos/{REPO}"
_OPEN = f"{_BASE}/code-scanning/alerts?state=open"
_PR_REF = "ref=refs%2Fpull%2F6096%2Fmerge"
_BRANCH_REF = "ref=refs%2Fheads%2Fdev"


def _alert(number: int, **over: Any) -> dict[str, Any]:
    """One alert as GitHub's REST API returns it, with fields the node must drop."""
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/{REPO}/security/code-scanning/{number}",
        "rule": {
            "id": "py/path-injection",
            "severity": "error",
            "security_severity_level": "high",
            "description": "dropped",
        },
        "tool": {"name": "CodeQL"},
        "most_recent_instance": {
            "ref": "refs/pull/6096/merge",
            "location": {"path": "src/a.py", "start_line": 12, "end_line": 14},
        },
        **over,
    }


def _req(**kw: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.CODE_SCANNING_ALERTS,
            "repo": REPO,
            **kw,
        }
    )


def _run(
    request: ModelGithubGatewayRequest, exchanges: list[JsonExchange]
) -> tuple[BaseModel, RecordedGatewayTransport]:
    transport = RecordedGatewayTransport(exchanges)
    result = dispatch(request, transport)
    transport.assert_drained()
    return result, transport


# --- golden chain ----------------------------------------------------------------


def test_code_scanning_golden_chain_pull_request_merge_ref() -> None:
    no_location = _alert(8, rule={"id": "js/xss"})
    no_location["most_recent_instance"] = {}
    result, transport = _run(
        _req(pr_number=6096),
        [
            JsonExchange(
                get(f"{_OPEN}&{_PR_REF}&per_page=30"),
                json_response([_alert(7), no_location]),
            )
        ],
    )
    assert isinstance(result, ModelCodeScanningAlertsResult)
    assert (result.repo, result.ref) == (REPO, "refs/pull/6096/merge")
    assert (result.available, result.unavailable_reason) == (True, None)
    assert result.truncated is False
    assert [a.model_dump() for a in result.alerts] == [
        {
            "number": 7,
            "rule_id": "py/path-injection",
            "severity": "error",
            "security_severity": "high",
            "path": "src/a.py",
            "line": 12,
            "html_url": f"https://github.com/{REPO}/security/code-scanning/7",
        },
        {
            "number": 8,
            "rule_id": "js/xss",
            "severity": None,
            "security_severity": None,
            "path": None,
            "line": None,
            "html_url": f"https://github.com/{REPO}/security/code-scanning/8",
        },
    ]
    assert len(transport.sent) == 1


def test_code_scanning_golden_chain_branch_ref_and_no_alerts() -> None:
    result, _ = _run(
        _req(branch="dev"),
        [JsonExchange(get(f"{_OPEN}&{_BRANCH_REF}&per_page=30"), json_response([]))],
    )
    assert isinstance(result, ModelCodeScanningAlertsResult)
    assert result.ref == "refs/heads/dev"
    assert (result.available, result.alerts, result.truncated) == (True, [], False)


def test_code_scanning_golden_chain_pages_and_reports_truncation() -> None:
    first = f"{_OPEN}&{_PR_REF}&per_page=100"
    second = f"{first}&page=2"
    result, transport = _run(
        _req(pr_number=6096, limit=150),
        [
            JsonExchange(
                get(first),
                json_response([_alert(i) for i in range(1, 101)], next_path=second),
            ),
            JsonExchange(
                get(second),
                json_response(
                    [_alert(i) for i in range(101, 201)],
                    next_path=f"{first}&page=3",
                ),
            ),
        ],
    )
    assert isinstance(result, ModelCodeScanningAlertsResult)
    assert [a.number for a in result.alerts] == list(range(1, 151))
    assert result.truncated is True
    assert len(transport.sent) == 2


def test_code_scanning_golden_chain_a_complete_list_is_not_truncated() -> None:
    first = f"{_OPEN}&{_PR_REF}&per_page=100"
    second = f"{first}&page=2"
    result, _ = _run(
        _req(pr_number=6096, limit=150),
        [
            JsonExchange(
                get(first),
                json_response([_alert(i) for i in range(1, 101)], next_path=second),
            ),
            JsonExchange(
                get(second), json_response([_alert(i) for i in range(101, 121)])
            ),
        ],
    )
    assert isinstance(result, ModelCodeScanningAlertsResult)
    assert len(result.alerts) == 120
    assert result.truncated is False


def test_code_scanning_golden_chain_result_round_trips_the_discriminator() -> None:
    result, _ = _run(
        _req(pr_number=6096),
        [
            JsonExchange(
                get(f"{_OPEN}&{_PR_REF}&per_page=30"), json_response([_alert(7)])
            )
        ],
    )
    wrapped = ModelGithubGatewayResponse.model_validate(
        {"correlation_id": "00000000-0000-4000-8000-000000020768", "result": result}
    )
    reparsed = ModelGithubGatewayResponse.model_validate_json(wrapped.model_dump_json())
    assert isinstance(reparsed.result, ModelCodeScanningAlertsResult)
    assert reparsed == wrapped


# --- error chain -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (404, "no analysis found", "no_analysis"),
        (404, "No analysis found for the ref", "no_analysis"),
        (
            404,
            "Advanced Security must be enabled for this repository to use code scanning.",
            "not_enabled",
        ),
        (404, "Not Found", "not_enabled"),
        (403, "Code scanning is not enabled for this repository", "not_enabled"),
        (
            403,
            "Advanced Security must be enabled for this repository to use code scanning.",
            "not_enabled",
        ),
        (403, "Resource not accessible by integration", "forbidden"),
    ],
)
def test_code_scanning_error_chain_unavailable_answers_name_their_reason(
    status: int, message: str, reason: str
) -> None:
    result, transport = _run(
        _req(pr_number=6096),
        [
            JsonExchange(
                get(f"{_OPEN}&{_PR_REF}&per_page=30"),
                json_response({"message": message}, status=status),
            )
        ],
    )
    assert isinstance(result, ModelCodeScanningAlertsResult)
    assert (result.available, result.unavailable_reason) == (False, reason)
    assert (result.alerts, result.truncated) == ([], False)
    assert len(transport.sent) == 1


def test_code_scanning_error_chain_other_statuses_raise() -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(f"{_OPEN}&{_PR_REF}&per_page=30"),
                json_response({"message": "Server Error"}, status=500),
            )
        ]
    )
    with pytest.raises(GitHubTransportError, match="HTTP 500"):
        dispatch(_req(pr_number=6096), transport)


def test_code_scanning_error_chain_a_transport_failure_propagates() -> None:
    class Broken:
        def send_sync(self, request: object) -> object:
            raise GitHubTransportError("GitHub request failed: connection reset")

    with pytest.raises(GitHubTransportError, match="connection reset"):
        dispatch(_req(pr_number=6096), cast(Any, Broken()))


@pytest.mark.parametrize(
    "fields",
    [
        {"available": False, "alerts": []},
        {"available": True, "unavailable_reason": "forbidden", "alerts": []},
        {
            "available": False,
            "unavailable_reason": "no_analysis",
            "alerts": [{"number": 1, "rule_id": "r"}],
        },
    ],
)
def test_code_scanning_error_chain_the_result_model_refuses_a_false_availability(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelCodeScanningAlertsResult.model_validate(
            {
                "operation": "code_scanning_alerts",
                "repo": REPO,
                "ref": "refs/heads/dev",
                "truncated": False,
                **fields,
            }
        )


# --- failing control -------------------------------------------------------------


def test_code_scanning_failing_control_neither_ref_is_refused() -> None:
    with pytest.raises(ValidationError, match="exactly one of pr_number and branch"):
        _req()


def test_code_scanning_failing_control_both_refs_are_refused() -> None:
    with pytest.raises(ValidationError, match="exactly one of pr_number and branch"):
        _req(pr_number=6096, branch="dev")


def test_code_scanning_failing_control_one_ref_is_accepted() -> None:
    """The positive control: the two refusals above come from the either/or rule."""
    assert _req(pr_number=6096).pr_number == 6096
    assert _req(branch="dev").branch == "dev"
