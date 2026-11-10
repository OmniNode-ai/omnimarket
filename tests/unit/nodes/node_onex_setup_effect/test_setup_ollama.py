# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ollama provisioning failure modes, specified before implementation.

Controlled vendor commands prove orchestration only. They must catch a verify
that installs, a foreign occupied port, inadequate disk/memory, unknown download
sizes, exit-zero without a binary/model, and a repair that repeats completed
downloads. Native inference remains a separate required proof.
"""

from pathlib import Path

import pytest

from omnimarket.models.delegation.model_ollama_config import ModelOllamaConfig
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_runtime import (
    HandlerSetupLocalRuntime,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def config() -> ModelOllamaConfig:
    return ModelOllamaConfig.model_validate(
        {
            "port": 11434,
            "chat_path": "/v1/chat/completions",
            "models": [
                {
                    "min_memory_gb": 16,
                    "model": "fixture-large:7b",
                    "download_gb": 5,
                    "parameter_count": "7B",
                    "context_window": 32768,
                    "max_tokens": 8192,
                },
                {
                    "min_memory_gb": 0,
                    "model": "fixture-small:1b",
                    "download_gb": 1,
                    "parameter_count": "1B",
                    "context_window": 8192,
                    "max_tokens": 2048,
                },
            ],
        }
    )


class Vendor:
    def __init__(self, tmp_path: Path) -> None:
        self.installed = False
        self.running = False
        self.foreign = False
        self.provision = True
        self.disk = 40
        self.memory = 16
        self.models: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self.app = tmp_path / "Ollama.app"
        self.app.mkdir()

    def locate(self, name: str) -> str | None:
        if name == "brew":
            return "/vendor/brew"
        return "/vendor/ollama" if name == "ollama" and self.installed else None

    def facts(self, _: Path) -> ModelSetupMachineFacts:
        return ModelSetupMachineFacts(
            platform="Darwin",
            architecture="arm64",
            macos_version="26.1",
            cpus=8,
            ram_bytes=self.memory * 1024**3,
            free_disk_bytes=self.disk * 1024**3,
            administrator=True,
            virtual_machine=False,
            rosetta=False,
        )

    def get_json(self, url: str) -> dict[str, object] | None:
        if not self.running:
            return None
        if url.endswith("/api/version"):
            return {"version": "0.40.16"}
        return {
            "models": [
                {"name": name, "digest": "sha256:fixture"} for name in self.models
            ]
        }

    def run(self, request: object) -> ModelSetupCommandResult:
        argv = request.argv
        self.commands.append(argv)
        if self.provision:
            if "install" in argv:
                self.installed = True
            elif argv[0] == "/usr/bin/open":
                self.running = True
            elif "pull" in argv:
                self.models.append(argv[-1])
        return ModelSetupCommandResult(argv=argv, returncode=0, stdout="", stderr="")

    def adapter(self) -> HandlerSetupLocalRuntime:
        return HandlerSetupLocalRuntime(
            collect=self.facts,
            locate=self.locate,
            run=self.run,
            get_json=self.get_json,
            port_open=lambda _: self.running or self.foreign,
            load_config=config,
            app_path=self.app,
            readiness_timeout_seconds=0.01,
        )


def plan(tmp_path: Path, *models: str, action: str = "install") -> ModelSetupPlan:
    return ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "workspace",
            "action": action,
            "providers": [
                {"provider": "ollama", "models": models or ("fixture-small:1b",)}
            ],
        }
    )


def test_unselected_provider_does_not_probe_or_install(tmp_path: Path) -> None:
    vendor = Vendor(tmp_path)
    result = vendor.adapter().handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "skipped"
    assert vendor.commands == []


def test_verify_is_read_only_and_reports_missing_model(tmp_path: Path) -> None:
    vendor = Vendor(tmp_path)
    result = vendor.adapter().handle(plan(tmp_path, action="verify"))
    assert result.status == "unavailable"
    assert vendor.commands == []


def test_install_and_repair_read_back_vendor_effects_and_preserve_order(
    tmp_path: Path,
) -> None:
    vendor = Vendor(tmp_path)
    selected = plan(tmp_path, "fixture-small:1b", "fixture-large:7b")
    result = vendor.adapter().handle(selected)
    assert result.status == "success"
    assert vendor.commands[0] == ("/vendor/brew", "install", "--cask", "ollama-app")
    assert [cmd[-1] for cmd in vendor.commands if "pull" in cmd] == list(
        selected.providers[0].models
    )
    before = list(vendor.commands)
    assert vendor.adapter().handle(selected).status == "success"
    assert vendor.commands == before


@pytest.mark.parametrize("failure", ["foreign-port", "disk", "memory", "unknown-model"])
def test_refusals_precede_vendor_commands(tmp_path: Path, failure: str) -> None:
    vendor = Vendor(tmp_path)
    models = ("fixture-small:1b",)
    if failure == "foreign-port":
        vendor.foreign = True
    elif failure == "disk":
        vendor.disk = 10
    elif failure == "memory":
        vendor.memory = 8
        models = ("fixture-large:7b",)
    else:
        models = ("not-in-the-declared-config",)
    assert vendor.adapter().handle(plan(tmp_path, *models)).status == "action_required"
    assert vendor.commands == []


def test_exit_zero_without_installed_binary_cannot_succeed(tmp_path: Path) -> None:
    vendor = Vendor(tmp_path)
    vendor.provision = False
    assert vendor.adapter().handle(plan(tmp_path)).status != "success"


def test_exit_zero_without_downloaded_model_cannot_succeed(tmp_path: Path) -> None:
    vendor = Vendor(tmp_path)
    vendor.installed = vendor.running = True
    vendor.provision = False
    assert vendor.adapter().handle(plan(tmp_path)).status != "success"
    assert [cmd for cmd in vendor.commands if "pull" in cmd]


def test_disk_consumed_by_the_app_is_rechecked_before_model_download(
    tmp_path: Path,
) -> None:
    vendor = Vendor(tmp_path)
    vendor.disk = 12
    original = vendor.run

    def install_consumes_space(request: object) -> ModelSetupCommandResult:
        result = original(request)
        if "install" in request.argv:
            vendor.disk = 10
        return result

    adapter = HandlerSetupLocalRuntime(
        collect=vendor.facts,
        locate=vendor.locate,
        run=install_consumes_space,
        get_json=vendor.get_json,
        port_open=lambda _: vendor.running or vendor.foreign,
        load_config=config,
        app_path=vendor.app,
        readiness_timeout_seconds=0.01,
    )
    assert adapter.handle(plan(tmp_path)).status == "action_required"
    assert not [cmd for cmd in vendor.commands if "pull" in cmd]
