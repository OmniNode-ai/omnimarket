# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A caller repository read and written through the shared GitHub transport (OMN-20134).

Reads are the contents API at the base branch head's sha. A branch is published
as one commit through the Git Data API (tree, commit, ref), so the pin line and
its test constant land together or not at all. No ``gh`` subprocess, no
credential in a request model: the transport adds it at send time.
"""

from __future__ import annotations

import base64
import binascii
from typing import Final, Literal, Protocol
from urllib.parse import quote

from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    ModelCallerSnapshot,
    ModelFileEdit,
    ModelPinCaller,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.protocols import (
    PinAdvanceError,
)

_BLOB_MODE: Final = "100644"


class ProtocolSyncGithubTransport(Protocol):
    def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse: ...


class GithubPinRepository:
    """ProtocolCallerRepository over the GitHub REST API."""

    def __init__(self, transport: ProtocolSyncGithubTransport) -> None:
        self._transport = transport

    def _send(
        self,
        method: Literal["GET", "POST"],
        repository: str,
        path: str,
        body: dict[str, object] | None,
    ) -> ModelGithubHttpResponse:
        request = ModelGithubHttpRequest(
            method=method,
            path=f"/repos/{repository}/{path}",
            body=body,
        )
        try:
            return self._transport.send_sync(request)
        except GithubLandingTransportError as exc:
            raise PinAdvanceError(f"{method} {request.path}: {exc}") from exc

    def _json(
        self,
        method: Literal["GET", "POST"],
        repository: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response = self._send(method, repository, path, body)
        if not 200 <= response.status < 300 or response.body is None:
            raise PinAdvanceError(
                f"{method} /repos/{repository}/{path}: HTTP {response.status} "
                f"{response.message()}"
            )
        return response.body

    @staticmethod
    def _sha_of(body: dict[str, object], key: str, where: str) -> str:
        node = body.get(key)
        sha = node.get("sha") if isinstance(node, dict) else body.get(key)
        if not isinstance(sha, str) or not sha:
            raise PinAdvanceError(f"{where}: no {key} sha in the answer")
        return sha

    def read_snapshot(self, caller: ModelPinCaller) -> ModelCallerSnapshot:
        repository = caller.repository
        ref = self._json(
            "GET", repository, f"git/ref/heads/{quote(caller.base_branch, safe='/')}"
        )
        head = self._sha_of(ref, "object", f"{repository} {caller.base_branch}")
        files: dict[str, str] = {}
        for site in (*caller.pin_sites, *caller.test_constants):
            if site.path in files:
                continue
            response = self._send(
                "GET", repository, f"contents/{quote(site.path)}?ref={head}", None
            )
            if response.status == 404:
                continue
            if response.status != 200 or response.body is None:
                raise PinAdvanceError(
                    f"read {repository}:{site.path}: HTTP {response.status} "
                    f"{response.message()}"
                )
            content = response.body.get("content")
            if response.body.get("encoding") != "base64" or not isinstance(
                content, str
            ):
                raise PinAdvanceError(f"read {repository}:{site.path}: not base64 text")
            try:
                files[site.path] = base64.b64decode(content).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise PinAdvanceError(
                    f"read {repository}:{site.path}: not UTF-8 text"
                ) from exc
        return ModelCallerSnapshot(
            repository=repository, base_head_sha=head, files=files
        )

    def publish_branch(
        self,
        *,
        repository: str,
        base_head_sha: str,
        branch: str,
        message: str,
        edits: tuple[ModelFileEdit, ...],
    ) -> str:
        existing = self._send(
            "GET", repository, f"git/ref/heads/{quote(branch, safe='/')}", None
        )
        if existing.status == 200 and existing.body is not None:
            return self._sha_of(existing.body, "object", f"{repository} {branch}")
        if existing.status != 404:
            raise PinAdvanceError(
                f"read {repository} branch {branch}: HTTP {existing.status} "
                f"{existing.message()}"
            )
        commit = self._json("GET", repository, f"git/commits/{base_head_sha}")
        base_tree = self._sha_of(commit, "tree", f"{repository} {base_head_sha}")
        tree = self._json(
            "POST",
            repository,
            "git/trees",
            {
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": edit.path,
                        "mode": _BLOB_MODE,
                        "type": "blob",
                        "content": edit.new_text,
                    }
                    for edit in edits
                ],
            },
        )
        created = self._json(
            "POST",
            repository,
            "git/commits",
            {
                "message": message,
                "tree": self._sha_of(tree, "sha", f"{repository} new tree"),
                "parents": [base_head_sha],
            },
        )
        new_head = self._sha_of(created, "sha", f"{repository} new commit")
        self._json(
            "POST",
            repository,
            "git/refs",
            {"ref": f"refs/heads/{branch}", "sha": new_head},
        )
        return new_head
