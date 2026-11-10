# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provision the official native Ollama app and declared models on this Mac.

The existing model config owns model tags and resource estimates. The existing
model effect owns routing and inference proof; this adapter proves only the
native service and downloaded models. It never removes a model or stops an
existing service.
"""

from __future__ import annotations

import shutil
import socket
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_ollama_config,
)
from omnimarket.models.delegation.model_ollama_config import (
    ModelOllamaConfig,
    ModelOllamaModelTier,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
    collect_machine_facts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


def _get_json(url: str) -> dict[str, object] | None:
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = client.get(url)
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                return None
            result = response.json()
        return result if isinstance(result, dict) else None
    except (httpx.HTTPError, ValueError, OSError):
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _locate(name: str) -> str | None:
    selected = shutil.which(name)
    if selected:
        return selected
    if name == "ollama":
        candidate = Path("/Applications/Ollama.app/Contents/Resources/ollama")
        if candidate.is_file():
            return str(candidate)
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.is_file():
            return str(candidate)
    return None


class HandlerSetupLocalRuntime:
    """Read-only observation plus bounded official-vendor provisioning."""

    name = "ollama"

    def __init__(
        self,
        *,
        collect: Callable[[Path], ModelSetupMachineFacts] | None = None,
        locate: Callable[[str], str | None] | None = None,
        run: Callable[[ModelSetupCommandRequest], ModelSetupCommandResult]
        | None = None,
        get_json: Callable[[str], dict[str, object] | None] | None = None,
        port_open: Callable[[int], bool] | None = None,
        load_config: Callable[[], ModelOllamaConfig | None] | None = None,
        app_path: Path = Path("/Applications/Ollama.app"),
        readiness_timeout_seconds: float = 120,
    ) -> None:
        self._collect = collect or collect_machine_facts
        self._locate = locate or _locate
        self._run = run or HandlerSetupCommand().handle
        self._json = get_json or _get_json
        self._port = port_open or _port_open
        self._load = load_config or load_ollama_config
        self._app = app_path
        self._timeout = readiness_timeout_seconds

    def _result(
        self, status: str, fix: str | None = None, *, changed: bool = False
    ) -> ModelSetupObservation:
        return ModelSetupObservation.model_validate(
            {"step": self.name, "status": status, "fix": fix, "changed": changed}
        )

    def validate_selection(self, plan: ModelSetupPlan) -> None:
        """Resolve declared tags and memory before CLI credentials or writes."""
        self._selection(plan)

    def _selection(
        self, plan: ModelSetupPlan
    ) -> tuple[ModelOllamaConfig, tuple[ModelOllamaModelTier, ...]] | None:
        selected = next((p for p in plan.providers if p.provider == "ollama"), None)
        if selected is None:
            return None
        config = self._load()
        if config is None:
            raise ValueError(
                "The installed model configuration declares no Ollama models."
            )
        if (
            selected.url
            and selected.url.rstrip("/") != f"http://127.0.0.1:{config.port}"
        ):
            raise ValueError(
                "Ollama setup runs on this Mac; use provider local for another server URL."
            )
        declared = {row.model: row for row in config.models}
        if any(name not in declared for name in selected.models):
            raise ValueError(
                "Choose Ollama models declared by the installed model config so memory and download requirements can be checked."
            )
        rows = tuple(declared[name] for name in selected.models)
        facts = self._collect(plan.workspace)
        if facts.ram_bytes is None or any(
            facts.ram_bytes < row.min_memory_gb * 1024**3 for row in rows
        ):
            raise ValueError(
                "The selected Ollama model exceeds this Mac's measured memory; choose a smaller declared model."
            )
        return config, rows

    def _version(self, base: str) -> str | None:
        value = self._json(base + "/api/version")
        version = value.get("version") if value else None
        return version if isinstance(version, str) and version else None

    def _models(self, base: str) -> set[str]:
        value = self._json(base + "/api/tags")
        models = value.get("models") if value else None
        if not isinstance(models, list):
            return set()
        return {
            row["name"]
            for row in models
            if isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and row.get("digest")
        }

    def _ready_name(self, name: str, observed: set[str]) -> bool:
        return name in observed or (":" not in name and name + ":latest" in observed)

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            selection = self._selection(plan)
            if selection is None:
                return self._result("skipped")
            config, rows = selection
            base = f"http://127.0.0.1:{config.port}"
            version = self._version(base)
            if version is None and self._port(config.port):
                return self._result(
                    "action_required",
                    "The Ollama port is occupied by an unrecognized service. Preserve it and choose provider local, or resolve the port before retrying.",
                )
            models = self._models(base) if version else set()
            missing = tuple(
                row for row in rows if not self._ready_name(row.model, models)
            )
            facts = self._collect(plan.workspace)
            needed = 10 * 1024**3 + sum(
                int(row.download_gb * 1024**3) for row in missing
            )
            if facts.free_disk_bytes < needed:
                return self._result(
                    "action_required",
                    "Free disk must retain the native 10 GiB reserve plus the selected model downloads before Ollama provisioning.",
                )
            if not self._locate("ollama") or not version or missing:
                return self._result(
                    "unavailable",
                    "Run onex setup --action repair to install/start Ollama and download the selected models.",
                )
            return ModelSetupObservation(
                step=self.name,
                status="success",
                evidence={
                    "endpoint": base,
                    "version": version,
                    "models": ",".join(row.model for row in rows),
                },
            )
        except ValueError as exc:
            return self._result("action_required", str(exc))

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        observation = self.observe(plan)
        if plan.action == "verify" or observation.status != "unavailable":
            return observation
        applied = self.apply(plan)
        return (
            applied
            if applied is not None
            else self.observe(plan).model_copy(update={"changed": True})
        )

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        before = self.observe(plan)
        if before.status != "unavailable":
            return before
        preflight = HandlerSetupPreflight(
            collect=self._collect, require_tools=False
        ).observe(plan)
        if preflight.status != "success":
            return self._result("action_required", preflight.fix)
        selection = self._selection(plan)
        if selection is None:
            return self._result("skipped")
        config, rows = selection
        base = f"http://127.0.0.1:{config.port}"
        binary = self._locate("ollama")
        if not binary:
            brew = self._locate("brew")
            if not brew:
                return self._result(
                    "action_required", "Install the base tools before Ollama."
                )
            result = self._run(
                ModelSetupCommandRequest(
                    argv=(brew, "install", "--cask", "ollama-app"), timeout_seconds=7200
                )
            )
            binary = self._locate("ollama")
            if result.returncode != 0 or not binary:
                return self._result(
                    "failure",
                    "The official Ollama cask did not produce its CLI. Review brew install --cask ollama-app, then repair.",
                    changed=True,
                )
        if not self._version(base):
            if not self._app.is_dir():
                return self._result(
                    "action_required",
                    "Start the existing native Ollama CLI with ollama serve, then repair; no unowned service was stopped.",
                    changed=True,
                )
            result = self._run(
                ModelSetupCommandRequest(
                    argv=("/usr/bin/open", "-a", str(self._app)), timeout_seconds=30
                )
            )
            deadline = time.monotonic() + self._timeout
            while (
                result.returncode == 0
                and self._version(base) is None
                and time.monotonic() < deadline
            ):
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            if not self._version(base):
                return self._result(
                    "action_required",
                    "Open Ollama once and finish any macOS prompt, then run repair.",
                    changed=True,
                )
        for row in rows:
            capacity = self.observe(plan)
            if capacity.status == "action_required":
                return capacity.model_copy(update={"changed": True})
            if self._ready_name(row.model, self._models(base)):
                continue
            result = self._run(
                ModelSetupCommandRequest(
                    argv=(binary, "pull", row.model), timeout_seconds=7200
                )
            )
            if result.returncode != 0 or not self._ready_name(
                row.model, self._models(base)
            ):
                return self._result(
                    "failure",
                    "Ollama did not report the requested model after pull. Repair will retain completed downloads and retry missing models.",
                    changed=True,
                )
        return None
