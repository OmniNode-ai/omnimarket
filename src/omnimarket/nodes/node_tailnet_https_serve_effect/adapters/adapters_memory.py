# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory default seams: the same protocols as the real ones, with no host behind them."""

from __future__ import annotations

from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    ModelInMemoryBackend,
    ModelServeResult,
)


class InMemoryUpstreamProbe:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.probed: list[str] = []

    def answers(self, upstream_url: str, path: str, timeout_seconds: int) -> bool:
        self.probed.append(f"{upstream_url}{path}")
        return self.alive


class InMemoryServeBackend:
    def __init__(self, served: dict[int, str] | None = None) -> None:
        self.served: dict[int, str] = dict(served or {})
        self.env_files: dict[str, str] = {}
        self.calls: list[str] = []

    @classmethod
    def from_overlay(cls, spec: ModelInMemoryBackend) -> InMemoryServeBackend:
        return cls(spec.served)

    def served_target(self, https_port: int) -> str:
        return self.served.get(https_port, "")

    def serve(self, https_port: int, upstream_url: str) -> None:
        self.calls.append(f"serve {https_port} {upstream_url}")
        self.served[https_port] = upstream_url

    def off(self, https_port: int) -> None:
        self.calls.append(f"off {https_port}")
        self.served.pop(https_port, None)

    def place_env(self, path: str, text: str) -> None:
        self.calls.append(f"place_env {path}")
        self.env_files[path] = text

    def remove_env(self, path: str) -> None:
        self.calls.append(f"remove_env {path}")
        self.env_files.pop(path, None)


class InMemoryReporter:
    def __init__(self) -> None:
        self.reports: list[ModelServeResult] = []

    def report(self, result: ModelServeResult) -> None:
        self.reports.append(result)
