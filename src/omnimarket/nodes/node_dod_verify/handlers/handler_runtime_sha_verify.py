# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerRuntimeShaVerify — probes .201 for deployed git SHA, writes DoD receipt. OMN-9356.

SSH git-SHA probe now; Docker label probe when OMN-9330 ships.
Produces ModelDodReceipt with check_type=runtime_sha_match and structured
actual_output (ModelRuntimeShaMatchOutput JSON) proving deployed_sha == merge_sha.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import UTC, datetime

from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.contracts.ticket.model_runtime_sha_match_output import (
    ModelRuntimeShaMatchOutput,
)
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH
from pydantic import BaseModel, ConfigDict, Field


class ModelRuntimeShaVerifyRequest(BaseModel):
    """Input to HandlerRuntimeShaVerify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    evidence_item_id: str = Field(..., min_length=1)
    merge_sha: str = Field(..., min_length=7, description="Git SHA merged to main")
    runtime_host: str = Field(
        ...,
        min_length=1,
        description="SSH host of the runtime server",
    )
    runtime_repo_path: str = Field(
        default="/opt/omninode/runtime",
        description="Absolute path to the deployed git repo on the runtime host",
    )
    runner: str = Field(default="integration-sweep-verifier", min_length=1)
    pr_number: int | None = Field(default=None, ge=1)
    ssh_timeout_s: int = Field(default=10, ge=1, le=60)


class HandlerRuntimeShaVerify:
    """Probes the runtime server for its deployed git SHA and produces a DoD receipt.

    Pure compute: given a request, SSH to the runtime host, read the deployed
    SHA via `git rev-parse HEAD`, compare to merge_sha, and return a
    ModelDodReceipt. No side effects beyond the SSH call.
    """

    def handle(self, request: ModelRuntimeShaVerifyRequest) -> ModelDodReceipt:
        """Run the SHA probe and return a PASS or FAIL receipt."""
        run_timestamp = datetime.now(tz=UTC)
        try:
            deployed_sha = self._probe_deployed_sha(request)
        except Exception as exc:
            return self._error_receipt(request, run_timestamp, str(exc))

        match = deployed_sha.lower() == request.merge_sha.lower()
        output = ModelRuntimeShaMatchOutput(
            runtime_host=request.runtime_host,
            deployed_sha=deployed_sha,
            merge_sha=request.merge_sha,
            match=match,
        )
        status = EnumReceiptStatus.PASS if match else EnumReceiptStatus.FAIL
        return ModelDodReceipt(
            ticket_id=request.ticket_id,
            evidence_item_id=request.evidence_item_id,
            check_type=CHECK_TYPE_RUNTIME_SHA_MATCH,
            check_value=self._probe_command(request),
            status=status,
            run_timestamp=run_timestamp,
            commit_sha=deployed_sha,
            runner=request.runner,
            actual_output=output.model_dump_json(),
            exit_code=0 if match else 1,
            pr_number=request.pr_number,
        )

    _SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

    def _probe_deployed_sha(self, request: ModelRuntimeShaVerifyRequest) -> str:
        """SSH to runtime host and return `git rev-parse HEAD` output.

        Uses subprocess list form (no shell=True) so host/path values are passed
        as argv elements, not shell-interpolated — no injection risk.

        Raises:
            RuntimeError: If SSH exits non-zero, output is empty, or output is
                not a valid lowercase hex SHA (7-40 chars).
        """
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={request.ssh_timeout_s}",
            "-o",
            "BatchMode=yes",
            request.runtime_host,
            f"git -C {shlex.quote(request.runtime_repo_path)} rev-parse HEAD",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=request.ssh_timeout_s + 5,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git rev-parse HEAD failed on {request.runtime_host} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        sha = result.stdout.strip().lower()
        if not sha:
            raise RuntimeError(
                f"git rev-parse HEAD returned empty output on {request.runtime_host}"
            )
        if not self._SHA_RE.match(sha):
            raise RuntimeError(
                f"git rev-parse HEAD returned unexpected output "
                f"(not a valid SHA): {sha!r}"
            )
        return sha

    def _probe_command(self, request: ModelRuntimeShaVerifyRequest) -> str:
        return (
            f"ssh {request.runtime_host} "
            f"git -C {request.runtime_repo_path} rev-parse HEAD"
        )

    def _error_receipt(
        self,
        request: ModelRuntimeShaVerifyRequest,
        run_timestamp: datetime,
        error_msg: str,
    ) -> ModelDodReceipt:
        error_output = json.dumps(
            {
                "runtime_host": request.runtime_host,
                "deployed_sha": "",
                "merge_sha": request.merge_sha,
                "match": False,
                "error": error_msg,
            }
        )
        return ModelDodReceipt(
            ticket_id=request.ticket_id,
            evidence_item_id=request.evidence_item_id,
            check_type=CHECK_TYPE_RUNTIME_SHA_MATCH,
            check_value=self._probe_command(request),
            status=EnumReceiptStatus.FAIL,
            run_timestamp=run_timestamp,
            commit_sha="0" * 7,
            runner=request.runner,
            actual_output=error_output,
            exit_code=1,
            pr_number=request.pr_number,
        )


__all__ = [
    "HandlerRuntimeShaVerify",
    "ModelRuntimeShaVerifyRequest",
]
