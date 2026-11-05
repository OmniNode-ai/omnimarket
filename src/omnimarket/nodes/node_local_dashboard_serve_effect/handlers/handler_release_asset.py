# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Download public assets with bounded retries for gateway responses."""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_local_dashboard_serve_effect.models.model_release_asset import (
    ModelReleaseAssetPolicy,
    ModelReleaseAssetRequest,
    ModelReleaseAssetResult,
)


class HandlerReleaseAsset:
    """The dashboard node's unauthenticated release-asset I/O boundary."""

    def handle(self, request: ModelReleaseAssetRequest) -> ModelReleaseAssetResult:
        if urllib.parse.urlsplit(request.url).scheme != "https":
            raise GitHubApiError(f"refusing a non-https asset URL: {request.url}")
        overlay = Path(__file__).resolve().parents[1] / "release_asset.overlay.yaml"
        policy = ModelReleaseAssetPolicy.model_validate(
            yaml.safe_load(overlay.read_text(encoding="utf-8"))
        )
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(request.url, method="GET")
        for attempt in range(1, policy.max_attempts + 1):
            try:
                with urllib.request.urlopen(
                    req, timeout=policy.timeout_seconds
                ) as resp:
                    content = resp.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                message = str(exc)
                exc.close()
                if (
                    status not in policy.retry_statuses
                    or attempt == policy.max_attempts
                ):
                    raise GitHubApiError(message, status_code=status) from exc
                time.sleep(policy.backoff_seconds * attempt)
                continue
            except (urllib.error.URLError, OSError) as exc:
                raise GitHubApiError(str(exc)) from exc
            try:
                request.destination.write_bytes(content)
            except OSError as exc:
                raise GitHubApiError(str(exc)) from exc
            return ModelReleaseAssetResult(
                destination=request.destination, attempts=attempt
            )
        raise AssertionError("validated retry policy allowed no attempt")
