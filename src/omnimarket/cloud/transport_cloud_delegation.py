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
    * ``429`` -> back-pressure, raised as :class:`CloudDelegationThrottledError`.
      On ``submit`` and ``receipt`` it stays what it always was: a refusal,
      surfaced immediately, never retried, because retrying a quota denial
      converts an instant, legible refusal into a timeout. On a STATUS POLL it
      is not a verdict on the workflow at all -- see below.
    * a connection failure -> distinguished from any refusal, because "your
      gateway refused you" and "your gateway is not there" have nothing in
      common operationally.

    A 5xx is reported as a 5xx: this is an interactive, single-shot customer
    command, not an unattended spooler, and a silent retry loop is precisely
    what hides the failure classes above.

WHY THE POLL LOOP IS THE ONE PLACE THAT BACKS OFF (OMN-18222)
    The gateway meters requests per tenant per minute -- 30/minute on the
    default ``discovery`` plan, over a 60-second sliding window. Polling status
    every 2 seconds is exactly 30 requests a minute, so the poll ALONE spent
    the entire plan and every delegation longer than about 40 seconds ended
    with a 429 on a poll. Treating that 429 as terminal reported a delegation
    that completed server-side as a failed one: measured twice on 2026-09-12,
    both runs finishing at the runtime ~52 seconds in, after the client had
    already given up.

    So a 429 on ``GET /status`` is back-pressure on the QUESTION, never an
    answer about the WORKFLOW. ``poll_until_terminal`` honours ``Retry-After``
    when the gateway sends one, backs off exponentially with jitter when it
    does not, and keeps asking until the workflow is terminal or the caller's
    wall-clock ceiling expires. The default cadence starts at 3 s and backs off
    toward 10 s, which costs at most half the discovery budget in steady state
    and leaves the rest for the customer's own submissions.

SECRET DISCIPLINE
    The key is held as ``SecretStr`` and read only at the header boundary. No
    request, header, body, or exception payload is logged, and no raised error
    interpolates the key or the response body's arbitrary content beyond the
    server's own ``detail`` string.
"""

from __future__ import annotations

import random
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
    "DEFAULT_MAX_POLL_INTERVAL_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "TERMINAL_STATUSES",
    "CloudDelegationThrottledError",
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

# The cadence a customer gets when they say nothing. 3 s -> 10 s costs between
# 6 and 20 requests a minute against a 30/minute plan, so the poll never owns
# more than about half the budget and a burst of it never owns all of it.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 3.0
DEFAULT_MAX_POLL_INTERVAL_SECONDS: Final[float] = 10.0

# How the interval grows between successive non-terminal polls. Geometric and
# gentle: a delegation that answers in 5 s is still answered in 5 s.
_POLL_BACKOFF_FACTOR: Final[float] = 1.5

# How the wait grows after a 429 that carried no ``Retry-After``. Doubling,
# capped below the gateway's own 60 s window: waiting longer than the window
# that is refusing you buys nothing, because the window has already rolled.
_THROTTLE_BACKOFF_FACTOR: Final[float] = 2.0
_THROTTLE_MAX_WAIT_SECONDS: Final[float] = 30.0

# Full-jitter band. Two clients throttled by the same window must not come back
# in lockstep and refuse each other again.
_JITTER_FLOOR: Final[float] = 0.5


class CloudDelegationThrottledError(ModelOnexError):
    """The gateway answered 429: back-pressure, not a verdict on the workflow.

    A subclass rather than a new error family, so every caller that already
    handles ``ModelOnexError`` with ``QUOTA_EXCEEDED`` is unchanged; only the
    poll loop, which is the one caller for which a 429 means something
    different, looks for the subclass.

    ``retry_after_seconds`` is the server's own instruction when it sent one.
    It is ``None`` when the header was absent or not a number of seconds -- the
    gateway's RPM middleware sends no such header today, which is exactly why
    the loop must own a backoff of its own rather than depending on one.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None) -> None:
        super().__init__(message, error_code=EnumCoreErrorCode.QUOTA_EXCEEDED)
        self.retry_after_seconds = retry_after_seconds


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
        deadline_seconds: float,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_interval_seconds: float = DEFAULT_MAX_POLL_INTERVAL_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        jitter_fn: Callable[[], float] = random.random,
    ) -> ModelCloudDelegationStatus:
        """Poll ``status`` until it is terminal or the wall-clock budget is spent.

        The budget is wall clock, not a poll count, because the cadence is not
        constant: it grows from ``interval_seconds`` toward
        ``max_interval_seconds``, and a throttled poll waits longer still. A
        count would mean a customer who asked for five minutes got a different
        number of minutes depending on how hard the gateway pushed back.

        Returns the last status observed. ``failed`` ends the loop on the spot:
        it is a terminal answer, and continuing to poll it would turn a legible
        runtime failure (a quota-dead model key, say) into an indistinguishable
        timeout.

        A 429 is NOT such an answer. It is the gateway declining to be asked,
        so the loop waits -- ``Retry-After`` if the gateway named one, an
        exponentially growing jittered wait if it did not -- and asks again.
        Throttled polls consume the budget like any other wait; they never end
        the loop, and they never produce a verdict.

        Args:
            workflow_id: The submitted workflow.
            deadline_seconds: Total wall-clock budget for reaching a terminal
                state, measured from the first poll.
            interval_seconds: The starting cadence.
            max_interval_seconds: The ceiling the cadence backs off toward.
            sleep_fn: Injected so tests do not wait.
            monotonic_fn: Injected so tests own the clock.
            jitter_fn: Returns a value in ``[0, 1)``. Injected so a test's
                backoff is exact rather than merely bounded.

        Raises:
            ModelOnexError: ``TIMEOUT_EXCEEDED`` if the budget runs out with the
                workflow still non-terminal. The workflow id is included so the
                customer can retrieve it later, and a run that spent its budget
                being throttled says so -- "still running" and "never asked"
                are different facts and the message must not merge them.
        """
        started_at = monotonic_fn()
        last: ModelCloudDelegationStatus | None = None
        interval = interval_seconds
        throttle_base: float | None = None
        throttled_polls = 0

        while True:
            try:
                last = self.status(workflow_id)
            except CloudDelegationThrottledError as throttled:
                throttled_polls += 1
                throttle_base = self._next_throttle_base(
                    previous=throttle_base, floor=interval
                )
                wait = self._throttle_wait(
                    retry_after_seconds=throttled.retry_after_seconds,
                    base=throttle_base,
                    jitter_fn=jitter_fn,
                )
            else:
                if last.status in TERMINAL_STATUSES:
                    return last
                # A poll the gateway answered means the window has room again.
                throttle_base = None
                wait = interval
                interval = min(interval * _POLL_BACKOFF_FACTOR, max_interval_seconds)

            remaining = deadline_seconds - (monotonic_fn() - started_at)
            if remaining <= 0.0:
                break
            sleep_fn(min(wait, remaining))

        observed = last.status if last is not None else "unknown"
        throttle_note = (
            f" {throttled_polls} poll(s) were refused by the gateway's rate "
            f"limit and waited out, which is back-pressure on the polling, not "
            f"a failure of the delegation."
            if throttled_polls > 0
            else ""
        )
        raise ModelOnexError(
            f"delegation {workflow_id} was still '{observed}' after "
            f"{deadline_seconds:g}s — it has NOT failed, it has not finished "
            f"yet. Retrieve it later with 'onex cloud receipt "
            f"{workflow_id}'.{throttle_note}",
            error_code=EnumCoreErrorCode.TIMEOUT_EXCEEDED,
        )

    @staticmethod
    def _next_throttle_base(*, previous: float | None, floor: float) -> float:
        """Grow the throttle wait, starting from the current poll cadence.

        Starting at the cadence rather than at a constant keeps a caller who
        deliberately polls slowly from being backed off to something faster
        than they asked for.
        """
        if previous is None:
            return min(
                max(floor, DEFAULT_POLL_INTERVAL_SECONDS), _THROTTLE_MAX_WAIT_SECONDS
            )
        return min(previous * _THROTTLE_BACKOFF_FACTOR, _THROTTLE_MAX_WAIT_SECONDS)

    @staticmethod
    def _throttle_wait(
        *,
        retry_after_seconds: float | None,
        base: float,
        jitter_fn: Callable[[], float],
    ) -> float:
        """The server's instruction if it gave one, a jittered backoff if not.

        ``Retry-After`` is obeyed as sent, with no jitter: it is an instruction
        about one window, not a guess, and spreading it would mean coming back
        before the server said to.
        """
        if retry_after_seconds is not None:
            return retry_after_seconds
        return base * (_JITTER_FLOOR + (1.0 - _JITTER_FLOOR) * jitter_fn())

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
            raise CloudDelegationThrottledError(
                f"the gateway answered 429 when asked to {operation} — a rate "
                f"limit or a plan quota. Gateway detail: {detail}",
                retry_after_seconds=self._retry_after_seconds(response),
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
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Read ``Retry-After`` as a delay in seconds, or report it absent.

        Only the delta-seconds form is read. The HTTP-date form is legal and
        this gateway does not send it; reporting an unread header as absent
        falls back to the loop's own backoff, which is the behaviour the header
        would have produced anyway. A negative or non-numeric value is likewise
        absent -- obeying "come back 5 seconds ago" is not obedience.
        """
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            return None
        return seconds if seconds >= 0.0 else None

    @staticmethod
    def _is_fenced(response: httpx.Response) -> bool:
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("fenced") is True
