# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin install-canary node; filesystem and process behavior lives in its handler."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .handlers.handler_install_canary import (
    HandlerInstallCanary,
    ModelInstallCanaryRequest,
    ModelInstallCanaryResult,
    canary_unit_repos,
)


class NodeInstallCanaryEffect:
    """Local entry point for the operations declared in contract.yaml."""

    contract_path = Path(__file__).with_name("contract.yaml")

    @property
    def default_repos(self) -> tuple[str, ...]:
        return canary_unit_repos()

    def handle(self, request: ModelInstallCanaryRequest) -> ModelInstallCanaryResult:
        return self.handler.handle(request)

    def __init__(self, runtime: Mapping[str, Any] | None = None) -> None:
        self.handler = HandlerInstallCanary(runtime)

    def pool_verdict(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.pool_verdict(*args, **kwargs)

    def cmd_canary_pool(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.cmd_canary_pool(*args, **kwargs)

    def canary_hold(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.canary_hold(*args, **kwargs)

    def adopt_marketplace(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.adopt_marketplace(*args, **kwargs)

    def canary_cap(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.canary_cap(*args, **kwargs)

    def merged_plugin_sha(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.merged_plugin_sha(*args, **kwargs)

    def installed_commit(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.installed_commit(*args, **kwargs)

    def publish_plugin_commit(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.publish_plugin_commit(*args, **kwargs)

    def ensure_plugin_trees(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.ensure_plugin_trees(*args, **kwargs)

    def tick_findings(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.tick_findings(*args, **kwargs)

    def receipt_plugins(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.receipt_plugins(*args, **kwargs)

    def lane_ship_drift(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.lane_ship_drift(*args, **kwargs)

    def shipped_plugin_sha(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.shipped_plugin_sha(*args, **kwargs)

    def scan_copy(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.scan_copy(*args, **kwargs)

    def tree_sha256(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.tree_sha256(*args, **kwargs)

    def pin(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.pin(*args, **kwargs)

    def read_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.read_bytes(*args, **kwargs)

    def copy_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.copy_bytes(*args, **kwargs)

    def write_atomic(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.write_atomic(*args, **kwargs)

    def keep_held(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.keep_held(*args, **kwargs)

    def restore_held(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.restore_held(*args, **kwargs)

    def source_path(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.source_path(*args, **kwargs)

    def stage_file(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.stage_file(*args, **kwargs)

    def install_stale(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.install_stale(*args, **kwargs)

    def install_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler.install_set(*args, **kwargs)
