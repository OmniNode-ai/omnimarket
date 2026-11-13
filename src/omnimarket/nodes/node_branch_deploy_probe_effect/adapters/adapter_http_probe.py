# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The HTTP runtime probe of node_branch_deploy_probe_effect."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact
from omnimarket.nodes.node_branch_deploy_probe_effect.overlay import (
    ModelHttpProbeSettings,
)

Fetch = Callable[[str, int], tuple[int, bytes]]


def fetch_http(url: str, timeout: int) -> tuple[int, bytes]:
    """The one network boundary of this probe; tests replace it."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _walk(document: Any, path: Sequence[str]) -> Any:
    node = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    return node


class HttpRuntimeProbe:
    """Reads the runtime's health and ready answers and its build provenance over HTTP."""

    def __init__(
        self, settings: ModelHttpProbeSettings, fetch: Fetch = fetch_http
    ) -> None:
        self._settings = settings
        self._fetch = fetch

    def read(
        self, now: datetime, expected_sha: str
    ) -> tuple[tuple[ModelRuntimeProbeFact, ...], str | None]:
        s = self._settings
        try:
            status, body = self._fetch(s.health_url, s.timeout_seconds)
            if status != 200:
                return (), f"the runtime health answer was HTTP {status}"
            healthy = _walk(json.loads(body), s.health_ok_path)
            if not isinstance(healthy, bool):
                raise ValueError("the health answer's serving flag is not a boolean")

            status, _ = self._fetch(s.ready_url, s.timeout_seconds)
            ready = 200 <= status < 300

            status, body = self._fetch(s.provenance_url, s.timeout_seconds)
            if status != 200:
                return (), f"the build provenance answer was HTTP {status}"
            observed = _walk(json.loads(body), s.sha_path)
            fact = ModelRuntimeProbeFact(
                probe_id=f"probe-{now.strftime('%Y%m%dT%H%M%SZ')}",
                probed_at=now,
                expected_sha=expected_sha,
                observed_sha=observed if isinstance(observed, str) else None,
                ok=healthy and ready,
            )
        except ValidationError:
            return (
                (),
                "runtime probe unreadable: the recorded build commit is not an exact sha",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return (), f"runtime probe unreadable: {type(exc).__name__}"
        return (fact,), None
