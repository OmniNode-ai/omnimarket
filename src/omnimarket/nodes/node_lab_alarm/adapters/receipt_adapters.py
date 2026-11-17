# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt readers of node_lab_alarm: the lane's delivered sha over HTTP, and the lab-pass receipt over the gateway.

The delivered sha comes from the lane's own agent and ready surfaces, never from an ambient variable; a surface that
does not answer, answers non-JSON or a non-object, or carries no matching sha is silent, never an error on its own.
The lab-pass receipt goes through the deployed gateway node that owns the source, never straight to it: the lane and
the timeout come from the overlay; this module holds neither. Nothing here prints or returns a credential or a
broker address: a failure is named by its exception type only.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping
from typing import Any

from omnimarket.handlers.scm.handler_gateway_read_client import (
    GatewayReadError,
    HandlerGatewayReadClient,
    _token_resolves,
    read_in_process,
    read_over_bus,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
)
from omnimarket.nodes.node_lab_alarm.adapters.bus_adapters import ProtocolGatewayReader
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabPassReceiptSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ReceiptAbsentError,
    ReceiptMalformedError,
    ReceiptSurfaceUnreadableError,
    ShaUnresolvedError,
)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class HttpDeliveredShaSource:
    """The lane's delivered sha: the agent's health document, else the ready surface's revision.

    A surface that does not answer, answers non-JSON or a non-object, or carries no matching sha is silent, never an
    error on its own; when every declared surface is silent, ``ShaUnresolvedError`` names the surfaces tried.
    """

    def __init__(
        self, agent_url: str, ready_url: str | None, timeout_seconds: float
    ) -> None:
        self._agent_url = agent_url
        self._ready_url = ready_url
        self._timeout_seconds = timeout_seconds

    def _get_json(self, url: str) -> dict[str, Any] | None:
        if not url.startswith("http://"):
            return None
        try:
            with (
                urllib.request.urlopen(  # no-contract-check: the lane's own sha read; port boundary, urls injected by the overlay-built reader
                    url, timeout=self._timeout_seconds
                ) as response
            ):
                if response.status != 200:
                    return None
                payload = json.loads(response.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ):
            return None
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def read(self) -> tuple[str, str]:
        tried: list[str] = []
        tried.append("deploy-agent health")
        health = self._get_json(f"{self._agent_url.rstrip('/')}/health")
        if health is not None:
            sha = health.get("loaded_code_sha")
            if isinstance(sha, str) and _SHA_PATTERN.match(sha):
                return sha, "deploy-agent loaded_code_sha"
        if self._ready_url is not None:
            tried.append("runtime /ready revision")
            ready = self._get_json(self._ready_url)
            if ready is not None:
                candidates: list[str] = []
                revision = ready.get("revision")
                if isinstance(revision, str):
                    candidates.append(revision)
                details = ready.get("details")
                if isinstance(details, dict):
                    detail_revision = details.get("revision")
                    if isinstance(detail_revision, str):
                        candidates.append(detail_revision)
                for candidate in candidates:
                    if _SHA_PATTERN.match(candidate):
                        return candidate, "runtime /ready revision"
        raise ShaUnresolvedError(
            f"no delivered sha from the declared surfaces: {', '.join(tried)}"
        )


class GatewayLabPassReceiptSource:
    """The lab-pass receipt: the completed runs, the unexpired artifact, its archive, and the receipt document."""

    def __init__(
        self,
        settings: ModelLabPassReceiptSettings,
        client: ProtocolGatewayReader | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HandlerGatewayReadClient(route=self._route)

    def _route(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        """In process where the gateway's own token resolves, else the deployed gateway over the bus within the overlay's timeout."""
        if _token_resolves():
            return read_in_process(request)
        return read_over_bus(request, timeout_s=self._settings.timeout_seconds)

    def read(self, sha: str) -> Mapping[str, Any]:
        settings = self._settings
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
            repo=settings.repository,
            workflow=settings.workflow,
            head_sha=sha,
            status="completed",
            limit=settings.runs_limit,
        )
        try:
            result = self._client.read(request)
        except GatewayReadError as exc:
            raise ReceiptSurfaceUnreadableError(str(exc)) from exc
        if not isinstance(result, ModelWorkflowRunsResult):
            raise ReceiptSurfaceUnreadableError(
                f"the gateway answered {type(result).__name__}, not workflow runs"
            )
        artifact_name = f"lab-pass-receipt-{settings.lane}-{sha}"
        candidates: list[tuple[str, int, ModelArtifactSummary]] = []
        for run in result.runs:
            artifacts_request = ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.ARTIFACTS,
                repo=settings.repository,
                run_id=run.id,
            )
            try:
                artifacts_result = self._client.read(artifacts_request)
            except GatewayReadError as exc:
                raise ReceiptSurfaceUnreadableError(str(exc)) from exc
            if not isinstance(artifacts_result, ModelArtifactsResult):
                raise ReceiptSurfaceUnreadableError(
                    f"the gateway answered {type(artifacts_result).__name__}, not artifacts"
                )
            for artifact in artifacts_result.artifacts:
                if artifact.name == artifact_name and not artifact.expired:
                    candidates.append((artifact.created_at or "", run.id, artifact))
        if not candidates:
            raise ReceiptAbsentError(artifact_name)
        # The newest created_at wins; an artifact with none sorts last.
        _, chosen_run_id, chosen_artifact = max(
            candidates, key=lambda candidate: candidate[0]
        )
        fetch_request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.ARTIFACTS,
            repo=settings.repository,
            run_id=chosen_run_id,
            artifact_id=chosen_artifact.id,
            max_bytes=settings.max_archive_bytes,
        )
        try:
            fetch_result = self._client.read(fetch_request)
        except GatewayReadError as exc:
            raise ReceiptSurfaceUnreadableError(str(exc)) from exc
        if not isinstance(fetch_result, ModelArtifactsResult):
            raise ReceiptSurfaceUnreadableError(
                f"the gateway answered {type(fetch_result).__name__}, not artifacts"
            )
        if fetch_result.fetch_refused or fetch_result.fetched is None:
            raise ReceiptMalformedError("the gateway refused the artifact fetch")
        try:
            content = base64.b64decode(fetch_result.fetched.content_base64)
        except (binascii.Error, ValueError) as exc:
            raise ReceiptMalformedError("the artifact is not base64") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = [
                    name for name in archive.namelist() if name.endswith("receipt.json")
                ]
                if len(members) != 1:
                    raise ReceiptMalformedError(
                        f"the archive holds {len(members)} receipt.json members, not one"
                    )
                receipt_bytes = archive.read(members[0])
        except zipfile.BadZipFile as exc:
            raise ReceiptMalformedError("the artifact is not a zip archive") from exc
        try:
            receipt = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReceiptMalformedError("the receipt is not UTF-8 JSON") from exc
        if not isinstance(receipt, dict):
            raise ReceiptMalformedError("the receipt is not a JSON object")
        return receipt


__all__ = [
    "GatewayLabPassReceiptSource",
    "HttpDeliveredShaSource",
]
