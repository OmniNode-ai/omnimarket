# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant-facing delegation client over the gateway's HTTPS surface (OMN-16967).

Three calls, in the order a customer makes them:

    POST /v1/workflows                      -> 202 + workflow_id
    GET  /v1/workflows/{id}/status          -> poll to a terminal status
    GET  /v1/workflows/{id}/receipt         -> the signed receipt + result

Doctrine placement. Customers never speak Kafka
(``feedback_customers_never_speak_kafka_gateway_only``); the gateway is the
external/tenant ingress and this is its client. That is why this lives beside
the gateway package's other client adapters rather than inside the
bus-oriented ``onex delegate`` path, whose transport is the event bus.

WHAT THIS DOES CLASSIFY -- and why, unlike ``gateway_transport_httpx``, it does
    That adapter deliberately hands every reached status back unclassified,
    because its two callers need different messages for the same code. This
    client has exactly one caller (``onex cloud``) and one audience (a beta
    customer at a terminal), so classification belongs here, once, where the
    HTTP semantics are known:

    * ``401``/``403`` -> the dashboard key was rejected. Named as such, with
      the remediation, and NEVER echoing the key.
    * ``400`` carrying ``fenced: true`` -> the gateway declares the workflow
      type but refuses to serve it (OMN-15365 fencing). A distinct message,
      because "not a real type" and "real type, deliberately not servable
      right now" call for different customer actions.
    * ``429`` / a quota refusal -> surfaced immediately and never retried.
      Retrying a quota denial converts an instant, legible refusal into a
      timeout, which is how a quota-dead account reads as a broken platform.
    * a connection failure -> distinguished from any refusal, because "your
      gateway refused you" and "your gateway is not there" have nothing in
      common operationally.

    Nothing here retries. A 5xx is reported as a 5xx: this is an interactive,
    single-shot customer command, not an unattended spooler, and a silent
    retry loop is precisely what hides the failure classes above.

SECRET DISCIPLINE
    The key is held as ``SecretStr`` and read only at the header boundary. No
    request, header, body, or exception payload is logged, and no raised error
    interpolates the key or the response body's arbitrary content beyond the
    server's own ``detail`` string.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Final

import httpx
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr

from omnimarket.cloud.model_cloud_delegation import (
    ModelCloudDelegationAck,
    ModelCloudDelegationReceipt,
    ModelCloudDelegationStatus,
)

__all__ = [
    "CLOUD_DELEGATION_WORKFLOW_TYPE",
    "TERMINAL_STATUSES",
    "TransportCloudDelegation",
]

# The one workflow type this client submits. Declared in the gateway's own
# allowlist (``docker/onex-api/workflow-contracts.yaml``); named here as a
# constant so the CLI cannot be talked into submitting an arbitrary type.
CLOUD_DELEGATION_WORKFLOW_TYPE: Final[str] = "delegation-inference"

# Lifecycle states from which a workflow never moves again
# (``routers/workflows.py::_TERMINAL_STATUSES``). Polling stops at either --
# including ``failed``, which is a result, not a reason to keep waiting.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed"})

_API_KEY_HEADER: Final[str] = "x-api-key"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_LOGIN_HINT: Final[str] = (
    "run 'onex cloud login --base-url <gateway origin> --api-key-stdin' with a "
    "key created in the dashboard"
)


class TransportCloudDelegation:
    """Submit, poll and retrieve one delegation over the gateway HTTPS API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Bind the client to one origin and one credential.

        Args:
            base_url: Gateway origin. Required, never defaulted — the caller
                resolves it from explicit configuration and refuses when unset.
            api_key: The dashboard-minted ``onxk_`` key.
            timeout_seconds: Per-request timeout.
            http_client: Injected transport. Supplied by tests
                (``httpx.MockTransport``); constructed here otherwise so the
                real path owns its own connection pool and closes it.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        # This module IS the EFFECT boundary for the `onex cloud` slice -- the
        # single component that opens a socket, exactly as
        # `omnibase_infra.gateway.client.gateway_transport_httpx` is for the
        # `onex auth` slice. Confining the client construction to this one line
        # is what keeps the credential store, the models and the CLI itself
        # transport-free and driveable by `httpx.MockTransport` with no network.
        # A customer's CLI calling the gateway over HTTPS has no bus-mediated
        # transport to route through -- customers never speak Kafka
        # (`feedback_customers_never_speak_kafka_gateway_only`), the gateway IS
        # their transport, and this is a client calling out, not a node
        # emitting. The no-contract-check tag is the scanner's sanctioned
        # per-line boundary annotation, NOT a path-allowlist broadening.
        self._client = (
            http_client
            if http_client is not None
            else httpx.Client(timeout=timeout_seconds)  # no-contract-check: the seam
        )
        self._owns_client = http_client is None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TransportCloudDelegation:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- calls -------------------------------------------------------------

    def submit(
        self, *, prompt: str, task_type: str, max_tokens: int | None
    ) -> ModelCloudDelegationAck:
        """Submit one delegation and return the gateway's acknowledgement.

        ``max_tokens`` is omitted from the payload entirely when ``None`` so
        the runtime resolves the response budget from its own routing contract
        rather than from a client-side default.
        """
        payload: dict[str, Any] = {"prompt": prompt, "task_type": task_type}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        body = {"workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE, "payload": payload}

        response = self._request(
            "POST", "/v1/workflows", json_body=body, operation="submit the delegation"
        )
        self._raise_for_status(response, operation="submit the delegation")
        return ModelCloudDelegationAck.model_validate(self._json(response))

    def status(self, workflow_id: str) -> ModelCloudDelegationStatus:
        """Read one workflow's current lifecycle state."""
        response = self._request(
            "GET",
            f"/v1/workflows/{workflow_id}/status",
            json_body=None,
            operation="read the delegation status",
        )
        self._raise_for_status(response, operation="read the delegation status")
        return ModelCloudDelegationStatus.model_validate(self._json(response))

    def poll_until_terminal(
        self,
        workflow_id: str,
        *,
        attempts: int,
        interval_seconds: float,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> ModelCloudDelegationStatus:
        """Poll ``status`` until it reaches a terminal state or attempts run out.

        Returns the last status observed. ``failed`` ends the loop on the spot:
        it is a terminal answer, and continuing to poll it would turn a legible
        runtime failure (a quota-dead model key, say) into an indistinguishable
        timeout.

        Raises:
            ModelOnexError: If the workflow is still non-terminal after
                ``attempts`` polls. A timeout is reported as a timeout — the
                workflow id is included so the customer can retrieve it later.
        """
        last: ModelCloudDelegationStatus | None = None
        for index in range(attempts):
            last = self.status(workflow_id)
            if last.status in TERMINAL_STATUSES:
                return last
            if index < attempts - 1:
                sleep_fn(interval_seconds)

        observed = last.status if last is not None else "unknown"
        raise ModelOnexError(
            f"delegation {workflow_id} was still '{observed}' after {attempts} "
            f"polls — it has NOT failed, it has not finished yet. Retrieve it "
            f"later with 'onex cloud receipt {workflow_id}'.",
            error_code=EnumCoreErrorCode.TIMEOUT_EXCEEDED,
        )

    def receipt(
        self, workflow_id: str, *, runner_identity: str
    ) -> ModelCloudDelegationReceipt:
        """Fetch the signed receipt for a terminal workflow.

        ``runner_identity`` is required by the endpoint and lands in the
        receipt's ``verifier`` field — who asked for this receipt is part of
        the receipt, not an implicit server-side guess.
        """
        response = self._request(
            "GET",
            f"/v1/workflows/{workflow_id}/receipt",
            json_body=None,
            operation="fetch the delegation receipt",
            params={"runner_identity": runner_identity},
        )
        self._raise_for_status(response, operation="fetch the delegation receipt")
        return ModelCloudDelegationReceipt.model_validate(self._json(response))

    # -- internals ---------------------------------------------------------

    def _headers(self, *, has_body: bool) -> dict[str, str]:
        headers = {
            _API_KEY_HEADER: self._api_key.get_secret_value(),
            "accept": "application/json",
        }
        if has_body:
            headers["content-type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None,
        operation: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one request, translating an unreached server into an error.

        A server that was not reached has no status to classify, and inventing
        one (a synthetic 503) would be indistinguishable from a real server
        answering 503 — the difference between "refused" and "not there".
        """
        try:
            return self._client.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                params=params,
                headers=self._headers(has_body=json_body is not None),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ModelOnexError(
                f"could not reach the OmniNode gateway at {self._base_url} to "
                f"{operation} ({type(exc).__name__}). Check the base URL and "
                "your network; this is not a credential problem.",
                error_code=EnumCoreErrorCode.NETWORK_ERROR,
            ) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelOnexError(
                f"the gateway answered {response.status_code} with a body that "
                "is not JSON.",
                error_code=EnumCoreErrorCode.PARSING_ERROR,
            ) from exc
        if not isinstance(body, dict):
            raise ModelOnexError(
                "the gateway answered with a JSON value that is not an object.",
                error_code=EnumCoreErrorCode.PARSING_ERROR,
            )
        return body

    def _raise_for_status(self, response: httpx.Response, *, operation: str) -> None:
        """Turn a non-2xx into the narrowest honest message for this caller."""
        if 200 <= response.status_code < 300:
            return

        detail = self._detail(response)

        if response.status_code in (401, 403):
            raise ModelOnexError(
                f"the gateway rejected the API key ({response.status_code}) "
                f"when asked to {operation}. The key may be revoked, may belong "
                f"to a different environment than {self._base_url}, or may have "
                f"been truncated on paste. To replace it, {_LOGIN_HINT}.",
                error_code=EnumCoreErrorCode.AUTHENTICATION_ERROR,
            )

        if response.status_code == 400 and self._is_fenced(response):
            raise ModelOnexError(
                f"the gateway declares workflow type "
                f"'{CLOUD_DELEGATION_WORKFLOW_TYPE}' but has it FENCED — it is "
                f"deliberately not servable right now, so this is an operator "
                f"state, not a fault in your request. Gateway detail: {detail}",
                error_code=EnumCoreErrorCode.UNSUPPORTED_OPERATION,
            )

        if response.status_code == 429:
            raise ModelOnexError(
                f"the gateway refused this delegation with 429 — a rate limit "
                f"or a plan quota, not a transient error. Gateway detail: "
                f"{detail}",
                error_code=EnumCoreErrorCode.QUOTA_EXCEEDED,
            )

        if 400 <= response.status_code < 500:
            raise ModelOnexError(
                f"the gateway refused the request to {operation} with "
                f"{response.status_code}. Gateway detail: {detail}",
                error_code=EnumCoreErrorCode.INVALID_INPUT,
            )

        raise ModelOnexError(
            f"the gateway failed to {operation} with {response.status_code}. "
            f"Gateway detail: {detail}",
            error_code=EnumCoreErrorCode.SERVICE_UNAVAILABLE,
        )

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """Extract the server's own ``detail`` string, never the whole body."""
        try:
            body = response.json()
        except ValueError:
            return "(no JSON body)"
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                return detail
        return "(no detail field)"

    @staticmethod
    def _is_fenced(response: httpx.Response) -> bool:
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("fenced") is True
