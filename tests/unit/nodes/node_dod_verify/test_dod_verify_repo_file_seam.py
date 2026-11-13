# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: node_dod_verify's ``repo_file`` read and node_github_repo_gateway_effect (OMN-24187).

The two sides share one request model, one result model and one operation name.
Here the dod_verify side sends its read through the real wire form (the request
as JSON, the gateway's own ``dispatch``, the gateway's own response wrapper as
JSON, the client's own result validation), so a field renamed on either side, a
result model dropped from the discriminated union, or an operation the gateway
contract no longer declares, fails this file and nothing else has to notice.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.scm import handler_gateway_read_client as client_mod
from omnimarket.handlers.scm.handler_gateway_read_client import (
    HandlerGatewayReadClient,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelRepoFileResult,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_github_repo_gateway_effect import dispatcher
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
)

pytestmark = pytest.mark.unit

_SHA = "c" * 40
_PATH = "contracts/OMN-90401.yaml"
_TEXT = "ticket_id: OMN-90401\ndod_evidence: []\n"
_CONTRACT_YAML = Path(dispatcher.__file__).resolve().parent / "contract.yaml"


def _file_exchange() -> JsonExchange:
    body = {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(_TEXT.encode()).decode(),
    }
    return JsonExchange(
        get(f"/repos/{REPO}/contents/{_PATH}?ref={_SHA}"), json_response(body)
    )


def _wire_route(
    transport: RecordedGatewayTransport, sent: list[str]
) -> client_mod.GatewayRoute:
    """The gateway as the bus carries it: JSON in, the node's dispatch, JSON out."""

    def route(request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        wire_request = request.model_dump_json()
        sent.append(wire_request)
        received = ModelGithubGatewayRequest.model_validate_json(wire_request)
        response = ModelGithubGatewayResponse(
            correlation_id=received.correlation_id,
            result=dispatcher.dispatch(received, transport),
        )
        payload = response.model_dump(mode="json")
        return client_mod._RESULT.validate_python(payload["result"])

    return route


def test_repo_file_seam_dod_verify_request_reaches_the_contents_read_and_back() -> None:
    transport = RecordedGatewayTransport([_file_exchange()])
    sent: list[str] = []
    handler = HandlerDodEvidenceGithubEffect(
        gateway=HandlerGatewayReadClient(route=_wire_route(transport, sent))
    )

    result = handler.gateway_repo_file(REPO, _SHA, _PATH)

    transport.assert_drained()
    assert isinstance(result, ModelRepoFileResult)
    assert (result.found, result.text) == (True, _TEXT)
    assert len(sent) == 1
    request = ModelGithubGatewayRequest.model_validate_json(sent[0])
    assert request.operation is EnumGithubGatewayOperation.REPO_FILE
    assert (request.repo, request.head_sha, request.file_path) == (REPO, _SHA, _PATH)


def test_repo_file_seam_a_404_crosses_the_wire_as_not_found() -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(f"/repos/{REPO}/contents/{_PATH}?ref={_SHA}"),
                json_response({}, status=404),
            )
        ]
    )
    handler = HandlerDodEvidenceGithubEffect(
        gateway=HandlerGatewayReadClient(route=_wire_route(transport, []))
    )

    result = handler.gateway_repo_file(REPO, _SHA, _PATH)

    assert isinstance(result, ModelRepoFileResult)
    assert (result.found, result.text) == (False, None)


def test_repo_file_seam_the_gateway_contract_declares_the_operation_dod_verify_sends() -> (
    None
):
    declared = {
        op["name"] for op in yaml.safe_load(_CONTRACT_YAML.read_text())["operations"]
    }
    assert EnumGithubGatewayOperation.REPO_FILE.value in declared
    assert (
        ModelRepoFileResult(
            repo=REPO, head_sha=_SHA, file_path=_PATH, found=False
        ).operation
        == EnumGithubGatewayOperation.REPO_FILE.value
    )
