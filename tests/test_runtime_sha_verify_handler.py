# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for runtime SHA verifier handler (OMN-9356).

TDD-first: tests written before implementation.
Verifier probes .201 runtime for deployed git SHA, compares to merge SHA,
and produces a ModelDodReceipt with check_type=runtime_sha_match.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.contracts.ticket.model_runtime_sha_match_output import (
    ModelRuntimeShaMatchOutput,
)
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

from omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify import (
    HandlerRuntimeShaVerify,
    ModelRuntimeShaVerifyRequest,
)

pytestmark = pytest.mark.unit

_MATCHING_SHA = "abc123def456"
_STALE_SHA = "deadbeef0000"


def _make_request(merge_sha: str = _MATCHING_SHA) -> ModelRuntimeShaVerifyRequest:
    return ModelRuntimeShaVerifyRequest(
        ticket_id="OMN-9356",
        evidence_item_id="dod-sha-001",
        merge_sha=merge_sha,
        runtime_host="192.168.86.201",  # onex-allow-internal-ip: test fixture
        runtime_repo_path="/opt/omninode/runtime",
        runner="test-runner",
        pr_number=999,
    )


class TestModelRuntimeShaVerifyRequest:
    def test_valid_request_constructs(self) -> None:
        req = _make_request()
        assert req.ticket_id == "OMN-9356"
        assert req.merge_sha == _MATCHING_SHA

    def test_missing_ticket_id_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelRuntimeShaVerifyRequest(
                evidence_item_id="dod-sha-001",
                merge_sha=_MATCHING_SHA,
                runtime_host="192.168.86.201",  # onex-allow-internal-ip: test fixture
                runtime_repo_path="/opt/omninode/runtime",
                runner="test",
            )


class TestHandlerRuntimeShaVerifyMatchingSha:
    def test_matching_sha_produces_pass_receipt(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_MATCHING_SHA):
            receipt = handler.handle(request)

        assert isinstance(receipt, ModelDodReceipt)
        assert receipt.status is EnumReceiptStatus.PASS
        assert receipt.check_type == CHECK_TYPE_RUNTIME_SHA_MATCH
        assert receipt.ticket_id == "OMN-9356"
        assert receipt.evidence_item_id == "dod-sha-001"

    def test_pass_receipt_actual_output_is_valid_json(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_MATCHING_SHA):
            receipt = handler.handle(request)

        assert receipt.actual_output is not None
        output = ModelRuntimeShaMatchOutput.model_validate(
            json.loads(receipt.actual_output)
        )
        assert output.match is True
        assert output.deployed_sha == _MATCHING_SHA
        assert output.merge_sha == _MATCHING_SHA

    def test_pass_receipt_commit_sha_matches_deployed(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_MATCHING_SHA):
            receipt = handler.handle(request)

        assert receipt.commit_sha == _MATCHING_SHA


class TestHandlerRuntimeShaVerifyStaleSha:
    def test_stale_runtime_produces_fail_receipt(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_STALE_SHA):
            receipt = handler.handle(request)

        assert receipt.status is EnumReceiptStatus.FAIL

    def test_fail_receipt_actual_output_shows_mismatch(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_STALE_SHA):
            receipt = handler.handle(request)

        output = ModelRuntimeShaMatchOutput.model_validate(
            json.loads(receipt.actual_output)
        )
        assert output.match is False
        assert output.deployed_sha == _STALE_SHA
        assert output.merge_sha == _MATCHING_SHA

    def test_fail_receipt_commit_sha_is_deployed_sha(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request(merge_sha=_MATCHING_SHA)

        with patch.object(handler, "_probe_deployed_sha", return_value=_STALE_SHA):
            receipt = handler.handle(request)

        assert receipt.commit_sha == _STALE_SHA


class TestHandlerRuntimeShaVerifyProbeFailure:
    def test_ssh_failure_produces_fail_receipt(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request()

        with patch.object(
            handler,
            "_probe_deployed_sha",
            side_effect=RuntimeError("SSH connection refused"),
        ):
            receipt = handler.handle(request)

        assert receipt.status is EnumReceiptStatus.FAIL
        assert receipt.actual_output is not None
        assert "error" in receipt.actual_output.lower() or receipt.actual_output

    def test_probe_error_receipt_has_error_in_output(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request()

        with patch.object(
            handler,
            "_probe_deployed_sha",
            side_effect=RuntimeError("Connection timeout"),
        ):
            receipt = handler.handle(request)

        assert receipt.status is EnumReceiptStatus.FAIL
        assert receipt.exit_code != 0


class TestHandlerRuntimeShaVerifyProbeMethod:
    def test_probe_calls_ssh_with_correct_command(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request()

        with patch(
            "omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=_MATCHING_SHA + "\n", stderr=""
            )
            sha = handler._probe_deployed_sha(request)

        assert sha == _MATCHING_SHA
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "192.168.86.201" in " ".join(
            cmd
        )  # onex-allow-internal-ip: test assertion
        assert "git" in " ".join(cmd)
        assert "rev-parse" in " ".join(cmd)

    def test_probe_raises_on_nonzero_exit(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request()

        with patch(
            "omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: not a git repository"
            )
            with pytest.raises(RuntimeError, match="git rev-parse"):
                handler._probe_deployed_sha(request)

    def test_probe_raises_on_non_sha_output(self) -> None:
        handler = HandlerRuntimeShaVerify()
        request = _make_request()

        with patch(
            "omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not-a-sha\n", stderr=""
            )
            with pytest.raises(RuntimeError, match="not a valid SHA"):
                handler._probe_deployed_sha(request)
