# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab verdict source the runtime binds for the lab state alarm's scheduled run (OMN-25461).

The Mac floor cycle reads eight lab sources from files under the registry root (the host record, the CI watcher state, the
pool receipt, the lab alarm run records, the projection bundle) and judges them with the lab-state compute. The runtime holds
none of those files, so only a source whose input the runtime can reach itself is read here: the dev deploy agent, asked over
HTTP at the address the lab-state overlay names. Every other declared source is left out of the verdicts and is named in
``unbound_kinds`` for the run event; a source nobody read is never reported WORKING, and is not alarmed as a failure of the
lab either, because the runtime never claimed to read it.

The overlay is the real ``node_floor_simple_effect`` document (the floor's lab-state overlay), so the sources, their age
bounds and the agent's address are the floor's own and no key is repeated in the schedule overlay.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import (
    ModelLabSourceVerdict,
    ModelLabStateInput,
    ModelLabStateOverlay,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources_deploy_agent import (
    http_fetch,
    read_deploy_agent,
    record_deploy_agent,
)

LAB_STATE_OVERLAY_ID = "node_floor_simple_effect"
RUNTIME_READABLE_KINDS: tuple[str, ...] = ("deploy_agent",)


class ProtocolDeployAgentFetch(Protocol):
    def __call__(self, url: str) -> Mapping[str, Any] | None: ...


class RuntimeLabVerdictSource:
    """Reads the lab sources the runtime can reach and judges them with the lab-state compute."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        overlay: ModelLabStateOverlay | None = None,
        fetch: ProtocolDeployAgentFetch | None = None,
    ) -> None:
        self._environ = environ
        self._overlay = overlay
        self._fetch = fetch
        self._record_dir = tempfile.TemporaryDirectory(prefix="lab-state-alarm-")
        self._unbound: tuple[str, ...] = ()

    @property
    def unbound_kinds(self) -> tuple[str, ...]:
        """The declared sources the last read left out, by id."""
        return self._unbound

    def _lab_overlay(self) -> ModelLabStateOverlay:
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                LAB_STATE_OVERLAY_ID, ModelLabStateOverlay, self._environ
            )
        return self._overlay

    def read(self, now: datetime) -> tuple[ModelLabSourceVerdict, ...] | None:
        overlay = self._lab_overlay()
        bound = tuple(s for s in overlay.sources if s.kind in RUNTIME_READABLE_KINDS)
        self._unbound = tuple(
            s.id for s in overlay.sources if s.kind not in RUNTIME_READABLE_KINDS
        )
        if not bound:
            return None
        record = Path(self._record_dir.name) / "deploy_agent.json"
        record_deploy_agent(
            record,
            overlay,
            self._fetch or http_fetch(overlay.deploy_agent_timeout_s),
            now,
        )
        reads = (read_deploy_agent(record, overlay, now),)
        return (
            HandlerLabState()
            .handle(ModelLabStateInput(now=now, sources=bound, reads=reads))
            .verdicts
        )


__all__: list[str] = [
    "LAB_STATE_OVERLAY_ID",
    "RUNTIME_READABLE_KINDS",
    "RuntimeLabVerdictSource",
]
