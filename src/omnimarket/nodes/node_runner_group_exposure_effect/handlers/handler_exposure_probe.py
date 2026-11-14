# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerExposureProbe: read one runner group, then ask the compute node for its reading.

EFFECT handler: reads only. The source (in-memory fixture or the GitHub REST API) and every setting
come from the overlay. A group that cannot be read never raises: the compute node reads it UNKNOWN
and the run ends in the typed failure event (``event_for``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.models.runner_group_exposure import (
    ModelRunnerGroupExposureRequest,
)
from omnimarket.nodes.node_runner_group_exposure_compute.handlers.handler_runner_group_exposure import (
    HandlerRunnerGroupExposure,
)
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.app_token_mint import (
    AppInstallationTokenMint,
)
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.github_group_source import (
    GithubGroupSource,
)
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.inmemory_group_source import (
    InMemoryGroupSource,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.http_transport import (
    UrllibHttpTransport,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
    ModelExposureProbeResult,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelExposureProbeOverlay,
    ModelInMemorySource,
    load_overlay,
)
from omnimarket.nodes.node_runner_group_exposure_effect.protocols.protocol_group_source import (
    ProtocolGroupSource,
    ProtocolHttpGet,
    ProtocolTokenMint,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics() -> dict[str, str]:
    """The topics by role, read from the contract (the handler holds no topic string)."""
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return dict(loaded["topics"])


class HandlerExposureProbe:
    def __init__(
        self,
        overlay: ModelExposureProbeOverlay | None = None,
        http_get: ProtocolHttpGet | None = None,
        environ: Mapping[str, str] | None = None,
        token_mint: ProtocolTokenMint | None = None,
    ) -> None:
        self._overlay = overlay
        self._http_get = http_get
        self._environ = environ
        self._token_mint = token_mint

    def _source(self, overlay: ModelExposureProbeOverlay) -> ProtocolGroupSource:
        source = overlay.source
        if isinstance(source, ModelInMemorySource):
            return InMemoryGroupSource(source.fixture)
        token_mint = self._token_mint
        if token_mint is None and source.token_mint is not None:
            store = source.token_mint.store
            token_mint = (
                AppInstallationTokenMint()
                if store is None
                else AppInstallationTokenMint(
                    reader=SecretStoreSettingsReader(overlay=store.secret_store),
                    folder=store.folder,
                )
            )
        return GithubGroupSource(
            source,
            self._http_get or UrllibHttpTransport(),
            os.environ if self._environ is None else self._environ,
            token_mint=token_mint,
        )

    def handle(self, request: ModelExposureProbeRequest) -> ModelExposureProbeResult:
        overlay = self._overlay or load_overlay(self._environ)
        read = self._source(overlay).read(now=request.now)
        reading = HandlerRunnerGroupExposure().handle(
            ModelRunnerGroupExposureRequest(
                correlation_id=request.correlation_id,
                now=request.now,
                read_at=read.read_at,
                settings=overlay.settings,
                group=read.group,
                files=read.files,
                jobs=read.jobs,
                read_failure=read.read_failure,
            )
        )
        return ModelExposureProbeResult(
            correlation_id=request.correlation_id,
            backend=overlay.source.backend,
            calls=read.calls,
            reading=reading,
            missing_permission=read.missing_permission,
        )

    @staticmethod
    def event_for(result: ModelExposureProbeResult) -> tuple[str, dict[str, Any]]:
        """The terminal event of a run: the failure topic when the group was not read."""
        topics = load_topics()
        topic = (
            topics["probe_failed"]
            if result.reading.read_failure is not None
            else topics["probe_completed"]
        )
        return topic, result.model_dump(mode="json")


__all__ = ["HandlerExposureProbe", "load_topics"]
