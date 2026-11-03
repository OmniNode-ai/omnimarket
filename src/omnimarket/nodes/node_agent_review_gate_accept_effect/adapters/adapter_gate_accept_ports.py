# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production ports a runtime gives the node when it constructs it without any.

``LocalStepFiles`` reads and appends real files on the machine the node runs on.
``GithubReviewReader`` reads a pull request's reviews through the source-control REST
API with the token and API address the runner already carries in its environment.
``HttpReceiptVerifier`` asks the OMN-22175 verifier, at the endpoint the node overlay
names, about one receipt. Both go through one injected HTTP transport, so a test never
reaches a network. ``SubprocessReceiptVerifier`` runs the same verifier node
(omniintelligence ``node_review_agent_receipt_verify_compute``) in a subprocess of an
omniintelligence checkout, with the key from ``LOCAL_LLM_SHARED_SECRET``; the overlay
selects it by a ``verifier_endpoint`` of the form ``subprocess:<checkout path>``, which
``EndpointReceiptVerifier`` (the default binding) routes. A binding whose environment is absent raises ``PortNotBoundError``
and the handler turns that into ``refuse``, which leaves the job red.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
    ModelHttpReply,
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
    ModelReviewView,
    ModelVerifierWireVerdict,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.protocols.protocol_gate_accept import (
    ProtocolHttpTransport,
    ProtocolReceiptVerifier,
)

_TOKEN_ENV = "GITHUB_TOKEN"
_API_ENV = "GITHUB_API_URL"
_KEY_ENV = "LOCAL_LLM_SHARED_SECRET"
SUBPROCESS_SCHEME = "subprocess:"


class PortNotBoundError(RuntimeError):
    """A port's environment is absent; the decision fails closed."""


class LocalStepFiles:
    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def append_text(self, path: str, text: str) -> None:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)


class UrllibHttpTransport:
    """One HTTP call over the standard library; any status is a reply, not an error."""

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float,
    ) -> ModelHttpReply:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(  # no-contract-check: port boundary
                request, timeout=timeout_seconds
            ) as reply:
                return ModelHttpReply(status=reply.status, body=reply.read())
        except urllib.error.HTTPError as exc:
            return ModelHttpReply(status=exc.code, body=exc.read())


class GithubReviewReader:
    """The pull request's reviews, read page by page with the runner's own token."""

    def __init__(
        self,
        *,
        transport: ProtocolHttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
        page_size: int = 100,
        max_pages: int = 10,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._transport: ProtocolHttpTransport = (
            transport if transport is not None else UrllibHttpTransport()
        )
        self._environ = environ
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout_seconds

    def reviews(self, repository: str, pull_request: int) -> list[ModelReviewView]:
        environ = os.environ if self._environ is None else self._environ
        token = environ.get(_TOKEN_ENV, "")
        api = environ.get(_API_ENV, "").rstrip("/")
        for name, value in ((_TOKEN_ENV, token), (_API_ENV, api)):
            if not value:
                raise PortNotBoundError(f"{name} is not set; no review reader is bound")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        found: list[ModelReviewView] = []
        for page in range(1, self._max_pages + 1):
            url = (
                f"{api}/repos/{repository}/pulls/{pull_request}/reviews"
                f"?per_page={self._page_size}&page={page}"
            )
            reply = self._transport.send(
                "GET", url, headers=headers, timeout_seconds=self._timeout
            )
            if reply.status != 200:
                raise RuntimeError(f"reading the reviews answered HTTP {reply.status}")
            document: Any = json.loads(reply.body)
            if not isinstance(document, list):
                raise RuntimeError("the reviews read is not a list")
            found.extend(_review(entry) for entry in document)
            if len(document) < self._page_size:
                return found
        raise RuntimeError(
            f"the pull request has more than {self._max_pages} pages of reviews"
        )


def _review(entry: Any) -> ModelReviewView:
    user = entry.get("user") if isinstance(entry, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return ModelReviewView(
        author_login=str(login or ""),
        commit_id=str(entry.get("commit_id") or ""),
        state=str(entry.get("state") or ""),
        body=str(entry.get("body") or ""),
    )


class HttpReceiptVerifier:
    """The OMN-22175 verifier, reached at the endpoint the node overlay names."""

    def __init__(
        self,
        *,
        transport: ProtocolHttpTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._transport: ProtocolHttpTransport = (
            transport if transport is not None else UrllibHttpTransport()
        )
        self._timeout = timeout_seconds

    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        body = json.dumps(
            {
                "receipt": request.receipt,
                "context": {
                    "repo": request.repository,
                    "pr": request.pull_request,
                    "head_sha": request.head_sha,
                },
            }
        ).encode("utf-8")
        reply = self._transport.send(
            "POST",
            request.verifier_endpoint,
            headers={"Content-Type": "application/json"},
            body=body,
            timeout_seconds=self._timeout,
        )
        if reply.status != 200:
            raise RuntimeError(f"the verifier answered HTTP {reply.status}")
        try:
            verdict = ModelVerifierWireVerdict.model_validate_json(reply.body)
        except ValidationError as exc:
            raise RuntimeError(
                f"the verifier's reply is not a verdict: {exc}"
            ) from None
        if verdict.decision == "accepted":
            return ModelReceiptVerifyOutcome(
                accepted=True, receipt_id=verdict.receipt_id or ""
            )
        return ModelReceiptVerifyOutcome(
            accepted=False, reason=verdict.reason or verdict.detail
        )


# Runs inside the omniintelligence checkout: the verifier node's own overlay loader,
# request and handler. The key is read from the child's environment and never printed.
VERIFY_PROGRAM = """
import json, os, sys
from omniintelligence.handlers.handler_review_agent_receipt_overlay import load_verifier_settings
from omniintelligence.nodes.node_review_agent_receipt_verify_compute.handlers.handler_review_agent_receipt_verify import HandlerReviewAgentReceiptVerify
from omniintelligence.nodes.node_review_agent_receipt_verify_compute.models.model_agent_receipt_verify_request import ModelAgentReceiptVerifyRequest
asked = json.load(sys.stdin)
key = os.environ.pop("LOCAL_LLM_SHARED_SECRET").encode("utf-8")
settings = load_verifier_settings()
try:
    request = ModelAgentReceiptVerifyRequest.model_validate({"receipt": asked["receipt"], "key": key, "policy": settings.policy, "quorum_size": settings.quorum_size, "context": asked["context"]})
except Exception as exc:
    sys.stdout.write(json.dumps({"decision": "refused", "reason": "malformed_receipt", "detail": type(exc).__name__}))
    raise SystemExit(0)
verdict = HandlerReviewAgentReceiptVerify().handle(request)
sys.stdout.write(json.dumps({"decision": str(getattr(verdict.decision, "value", verdict.decision)), "reason": None if verdict.reason is None else str(getattr(verdict.reason, "value", verdict.reason)), "detail": verdict.detail, "receipt_id": None if verdict.receipt_id is None else str(verdict.receipt_id)}))
"""


def _outcome(verdict: ModelVerifierWireVerdict) -> ModelReceiptVerifyOutcome:
    if verdict.decision == "accepted":
        return ModelReceiptVerifyOutcome(
            accepted=True, receipt_id=verdict.receipt_id or ""
        )
    return ModelReceiptVerifyOutcome(
        accepted=False, reason=verdict.reason or verdict.detail
    )


class SubprocessReceiptVerifier:
    """The OMN-22175 verifier node, run in a subprocess of an omniintelligence checkout.

    No new service and no new secret: the Hostile Reviewer job already clones the
    checkout (``../omniintelligence``) and carries ``LOCAL_LLM_SHARED_SECRET``. An absent
    key or checkout raises ``PortNotBoundError``; any other failure raises, and the
    handler refuses.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self._environ = environ
        self._timeout = timeout_seconds

    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        environ = os.environ if self._environ is None else self._environ
        key = environ.get(_KEY_ENV, "")
        if not key:
            raise PortNotBoundError(f"{_KEY_ENV} is not set; no verifier is bound")
        checkout = Path(request.verifier_endpoint.removeprefix(SUBPROCESS_SCHEME))
        if not (checkout / "pyproject.toml").is_file():
            raise PortNotBoundError(
                f"{checkout} is not an omniintelligence checkout; no verifier is bound"
            )
        child = {k: v for k, v in environ.items() if k != "VIRTUAL_ENV"}
        child[_KEY_ENV] = key
        completed = subprocess.run(  # no-contract-check: port boundary
            ["uv", "run", "--frozen", "python", "-c", VERIFY_PROGRAM],
            cwd=checkout,
            input=json.dumps(
                {
                    "receipt": request.receipt,
                    "context": {
                        "repo": request.repository,
                        "pr": request.pull_request,
                        "head_sha": request.head_sha,
                    },
                }
            ),
            capture_output=True,
            text=True,
            env=child,
            timeout=self._timeout,
            check=False,
        )
        if completed.returncode != 0:
            tail = (completed.stderr.strip().splitlines() or [""])[-1]
            raise RuntimeError(f"the verifier exited {completed.returncode}: {tail}")
        try:
            verdict = ModelVerifierWireVerdict.model_validate_json(completed.stdout)
        except ValidationError as exc:
            raise RuntimeError(
                f"the verifier's reply is not a verdict: {exc}"
            ) from None
        return _outcome(verdict)


class EndpointReceiptVerifier:
    """The default binding: the overlay's ``verifier_endpoint`` picks the verifier."""

    def __init__(
        self,
        *,
        http: ProtocolReceiptVerifier | None = None,
        subprocess_verifier: ProtocolReceiptVerifier | None = None,
    ) -> None:
        self._http: ProtocolReceiptVerifier = (
            http if http is not None else HttpReceiptVerifier()
        )
        self._subprocess: ProtocolReceiptVerifier = (
            subprocess_verifier
            if subprocess_verifier is not None
            else SubprocessReceiptVerifier()
        )

    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        if request.verifier_endpoint.startswith(SUBPROCESS_SCHEME):
            return self._subprocess.verify(request)
        return self._http.verify(request)
