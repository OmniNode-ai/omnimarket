# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs one delegation end to end through a gateway and reads the answering model off the receipt.

The gateway URL comes from ``ONEX_API_BASE_URL`` and the key from the owner-only file ``ONEX_API_KEY_FILE`` names, the same two
settings ``onex cloud`` reads; there is no default for either. Every failure is a typed outcome with a reason, and nothing here
logs or returns the key.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Protocol

from omnimarket.nodes.node_cloud_smoke_check_effect.models.model_cloud_smoke_check import (
    EnumCloudSmokeOutcome,
    ModelCloudSmokeCommand,
    ModelCloudSmokeResult,
    ModelGatewayAnswer,
)

logger = logging.getLogger(__name__)

URL_SETTING: Final = "ONEX_API_BASE_URL"
KEY_FILE_SETTING: Final = "ONEX_API_KEY_FILE"
WORKFLOW_TYPE: Final = "delegate-skill"
TERMINAL: Final = frozenset({"completed", "failed"})
RUNNER_IDENTITY: Final = "cloud-smoke-check"
RECEIPT_QUERY: Final = f"?runner_identity={RUNNER_IDENTITY}"


class ProtocolGatewayTransport(Protocol):
    def call(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict[str, Any] | None,
    ) -> ModelGatewayAnswer: ...


API_KEY_HEADER: Final = "x-api-key"
BODY_LIMIT: Final = 1 << 20


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None


class HttpGatewayTransport:
    """The real transport, defined in the effect handler module so the imperative contract guard treats its HTTP call as the effect I/O boundary. Sends a request to ``base_url + path``; an unanswered request is an answer with an error type and no message."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self._timeout_s = timeout_s

    def call(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict[str, Any] | None,
    ) -> ModelGatewayAnswer:
        headers = {"accept": "application/json", API_KEY_HEADER: api_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return ModelGatewayAnswer(
                    status=response.status, body=_parse(response.read(BODY_LIMIT))
                )
        except urllib.error.HTTPError as exc:
            return ModelGatewayAnswer(
                status=exc.code, body=_parse(exc.read(BODY_LIMIT))
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The type only: a message can echo the request, and a request carries the key.
            return ModelGatewayAnswer(status=None, error=type(exc).__name__)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class HandlerCloudSmokeCheck:
    """Submit one delegation, wait for it to end, read its receipt, and pass only when the receipt names the model."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        transport: ProtocolGatewayTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._transport: ProtocolGatewayTransport = transport or HttpGatewayTransport()
        self._sleep = sleep
        self._monotonic = monotonic

    @staticmethod
    def _result(
        outcome: EnumCloudSmokeOutcome, detail: str, **fields: str | None
    ) -> ModelCloudSmokeResult:
        logger.info("cloud smoke check outcome=%s", outcome.value)
        return ModelCloudSmokeResult(
            ok=outcome is EnumCloudSmokeOutcome.PASSED,
            outcome=outcome,
            detail=detail,
            **fields,
        )

    def _read_key(self, path: Path) -> str | ModelCloudSmokeResult:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            return self._result(
                EnumCloudSmokeOutcome.KEY_FILE_REFUSED,
                f"the file {KEY_FILE_SETTING} names does not exist or cannot be read",
            )
        if mode & 0o077:
            return self._result(
                EnumCloudSmokeOutcome.KEY_FILE_REFUSED,
                f"the key file is mode {mode:04o}; it must be owner-only (0600)",
            )
        try:
            key = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return self._result(
                EnumCloudSmokeOutcome.KEY_FILE_REFUSED, "the key file cannot be read"
            )
        if not key:
            return self._result(
                EnumCloudSmokeOutcome.KEY_FILE_REFUSED, "the key file is empty"
            )
        return key

    def _refused(self, step: str, answer: ModelGatewayAnswer) -> ModelCloudSmokeResult:
        status = answer.status
        if status is None:
            return self._result(
                EnumCloudSmokeOutcome.GATEWAY_UNREACHABLE,
                f"{step} got no answer: {answer.error or 'no error named'}",
            )
        if status == 503:
            return self._result(
                EnumCloudSmokeOutcome.GATEWAY_UNAVAILABLE,
                f"{step} answered 503: the gateway is up but not serving; see the runbook's 503 section",
            )
        if status in (401, 403):
            return self._result(
                EnumCloudSmokeOutcome.AUTH_REFUSED,
                f"{step} answered {status}: the gateway refused the key",
            )
        return self._result(
            EnumCloudSmokeOutcome.SUBMIT_REFUSED, f"{step} answered {status}"
        )

    async def handle(self, request: ModelCloudSmokeCommand) -> ModelCloudSmokeResult:
        base_url = (self._environ.get(URL_SETTING) or "").strip()
        key_file = (self._environ.get(KEY_FILE_SETTING) or "").strip()
        missing = [
            name
            for name, value in ((URL_SETTING, base_url), (KEY_FILE_SETTING, key_file))
            if not value
        ]
        if missing:
            return self._result(
                EnumCloudSmokeOutcome.SETTINGS_ABSENT,
                f"no gateway settings: {' and '.join(missing)} not set "
                "(there is no default gateway and no default key)",
            )
        key = self._read_key(Path(key_file))
        if isinstance(key, ModelCloudSmokeResult):
            return key

        submitted = self._transport.call(
            base_url,
            "POST",
            "/v1/workflows",
            api_key=key,
            body={
                "workflow_type": WORKFLOW_TYPE,
                "correlation_id": str(uuid.uuid4()),
                "payload": {
                    "prompt": request.prompt,
                    "task_type": request.task_type,
                    "source": "external-client",
                    "max_tokens": request.max_tokens,
                },
            },
        )
        body = submitted.body
        workflow_id = _text(body.get("workflow_id")) if isinstance(body, dict) else None
        if submitted.status != 202 or workflow_id is None:
            return self._refused("submit", submitted)

        deadline = self._monotonic() + request.run_budget_s
        status_body: Mapping[str, Any] | None = None
        while True:
            polled = self._transport.call(
                base_url,
                "GET",
                f"/v1/workflows/{workflow_id}/status",
                api_key=key,
                body=None,
            )
            if (
                polled.status is None
                or polled.status >= 500
                or polled.status in (401, 403)
            ):
                return self._refused("status read", polled)
            if polled.status == 200 and isinstance(polled.body, dict):
                status_body = polled.body
                if status_body.get("status") in TERMINAL:
                    break
            if self._monotonic() >= deadline:
                return self._result(
                    EnumCloudSmokeOutcome.RUN_TIMED_OUT,
                    f"the run did not end within {request.run_budget_s:g} s "
                    f"(last status {(status_body or {}).get('status')!r})",
                    workflow_id=workflow_id,
                )
            self._sleep(request.poll_interval_s)

        if status_body.get("status") != "completed":
            code = _text(status_body.get("terminal_failure_code")) or "no code named"
            return self._result(
                EnumCloudSmokeOutcome.RUN_FAILED,
                f"the run ended failed: {code}",
                workflow_id=workflow_id,
            )

        got = self._transport.call(
            base_url,
            "GET",
            f"/v1/workflows/{workflow_id}/receipt{RECEIPT_QUERY}",
            api_key=key,
            body=None,
        )
        if got.status != 200 or not isinstance(got.body, dict):
            return self._result(
                EnumCloudSmokeOutcome.RECEIPT_UNREADABLE,
                f"the receipt read answered {got.status if got.status is not None else got.error}",
                workflow_id=workflow_id,
            )
        model = _text(got.body.get("terminal_model_used"))
        provider = _text(got.body.get("provider"))
        route = _text(got.body.get("route"))
        if model is None:
            return self._result(
                EnumCloudSmokeOutcome.RECEIPT_NAMES_NO_MODEL,
                "the receipt does not name the model that answered",
                workflow_id=workflow_id,
                provider=provider,
                route=route,
            )
        return self._result(
            EnumCloudSmokeOutcome.PASSED,
            "one delegation ran end to end and its receipt names the answering model",
            workflow_id=workflow_id,
            model=model,
            provider=provider,
            route=route,
        )
